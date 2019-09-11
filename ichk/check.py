"""Scan and check resource or vault"""

from __future__ import print_function
import sys
import os
import errno
from enum import Enum
from collections import namedtuple
from itertools import chain
import hashlib
import base64
from ichk.formatters import Formatter
from irods.column import Like
from irods.models import Resource, Collection, DataObject
import irods.exception as iexc

CHUNK_SIZE = 8192
PY2 = (sys.version_info.major == 2)

class Status(Enum):
    OK = 0
    NOT_EXISTING = 1        # File registered in iRODS but not found in vault
    NOT_REGISTERED = 2      # File found in vault but is not registered in iRODS
    FILE_SIZE_MISMATCH = 3  # File sizes do not match between database and vault
    CHECKSUM_MISMATCH = 4   # Checksums do not match between database and vault
    ACCESS_DENIED = 5       # This script was denied access to the file
    NO_CHECKSUM = 6         # iRODS has no checksum registered

    def __repr__(self):
        return self.name

class ObjectType(Enum):
    COLLECTION = 0
    DATAOBJECT = 1
    FILE = 2
    DIRECTORY = 3

Result = namedtuple('Result', 'obj_type obj_path phy_path status observed_values')


def on_disk(path):

    try:
        os.stat(path)
    except OSError as e:
        if e.errno == errno.ENOENT:
            return Status.NOT_EXISTING
        elif e.errno == errno.EACCES:
            return Status.ACCESS_DENIED
        else:
            raise

    return Status.OK


class ObjectChecker(object):

    def __init__(self, data_object, phy_path):
        self.data_object = data_object
        self.obj_path = data_object[DataObject.path]
        self.phy_path = phy_path
        self._statinfo = None

    @property
    def statinfo(self):
        if self._statinfo:
            return self._statinfo
        try:
            statinfo = os.stat(self.phy_path)
        except OSError as e:
            if e.errno == errno.ENOENT:
                statinfo = Status.NOT_EXISTING
            elif e.errno == errno.EACCES:
                statinfo = Status.ACCESS_DENIED
            else:
                raise

        self._statinfo = statinfo
        return statinfo

    def exists_on_disk(self):
        if isinstance(self.statinfo, Status):
            return self.statinfo
        return Status.OK

    def compare_filesize(self):
        # TODO: what about sparse files?
        data_object_size = self.data_object[DataObject.size]
        if isinstance(self.statinfo, Status):
            return self.statinfo

        info = {
            'expected_filesize': str(data_object_size),
            'observed_filesize': str(self.statinfo.st_size) }

        if data_object_size != self.statinfo.st_size:
            return Status.FILE_SIZE_MISMATCH, info

        return Status.OK, info

    def compare_checksums(self):
        irods_checksum = self.data_object[DataObject.checksum]
        info = {
            'expected_checksum': None,
            'observed_checksum': None }
        if not irods_checksum:
            return Status.NO_CHECKSUM, info

        try:
            f = open(self.phy_path, 'rb')
        except OSError as e:
            if e.errno == errno.EACCES:
                return Status.ACCESS_DENIED, info
            else:
                raise
        else:
            # iRODS returns sha256 checksums as base64 encoded string of
            # the hash prefixed with sha2 and seperated
            # by a colon, ':'.
            # md5 checksums are not prefixed and not base64 encoded.
            if irods_checksum.startswith("sha2:"):
                irods_checksum = irods_checksum[5:]
                hsh = hashlib.sha256()
            else:
                hsh = hashlib.md5()

            while True:
                chunk = f.read(CHUNK_SIZE)
                if chunk:
                    hsh.update(chunk)
                else:
                    break

            if hsh.name == 'md5':
                phy_checksum = hsh.digest()
            else:
                phy_checksum = base64.b64encode(hsh.digest())
            f.close()

        info = {
            'expected_checksum': irods_checksum,
            'observed_checksum': phy_checksum }

        if phy_checksum != irods_checksum:
            return Status.CHECKSUM_MISMATCH, info

        return Status.OK, info


class Check(object):

    def __init__(self, session, fqdn, root_collection):
        self.fqdn = fqdn
        self.session = session
        if root_collection is not None:
            found_collection = (self.session.query(Collection.id, Collection.name)
                        .filter(Collection.name == root_collection)
                        .get_results())
            if len(list(found_collection)) != 1:
                raise ValueError("Root collection {} not found.".format(root_collection))

        self.root_collection = root_collection

    def setformatter(self, output=None, fmt=None, show_size_checksum=False, **options):
        """Use different formatters based on fmt Argument"""
        if (output is None) or (fmt is None):
            raise ValueError(
                "Check.setformatter needs an output and fmt argument")

        formatters = Formatter.__subclasses__()
        for formatter in formatters:
            if formatter.name == fmt:
                self.formatter = formatter(output, show_size_checksum, **options)
                break
        else:
            raise ValueError("Unknown formatter: {}".format(fmt))

    def get_resource(self, resource_name):
        try:
            resource = self.session.query(Resource).filter(
                Resource.name == resource_name).one()
        except iexc.NoResultFound:
            print("No result found for a resource named: {resource_name}"
                  .format(**locals()),
                  file=sys.stderr)
            resource = None
        return resource

    def get_resource_from_phy_path(self, phy_path):
        try:
            resource = (self.session.query(Resource)
                        .filter(Resource.vault_path == phy_path)
                        .filter(Resource.location == self.fqdn)
                        .one()
                        )
        except iexc.NoResultFound:
            resource = None
        return resource

    @property
    def vault(self):
        if self._vault:
            return self._vault
        else:
            return None

    @vault.setter
    def vault(self, vault_path):
        if os.path.exists(vault_path):
            self._vault = os.path.realpath(vault_path)
        else:
            sys.exit("Vault path {vault_path} does not exist"
                     .format(**locals()))

    def find_root(self, resource):
        ancestors = []

        def climb(resource):
            parent = resource[Resource.parent]
            if parent is None:
                print("Root resource is {}".format(resource[Resource.name]),
                      file=sys.stderr)
                return resource
            else:
                ancestor = (
                    self.session.query(Resource.id, Resource.name)
                    .filter(Resource.id == parent)
                    .one()
                    )
                ancestors.append(ancestor[Resource.name])
                return climb(self.get_resource(ancestor[Resource.name]))

        root = climb(resource)
        ancestors.reverse()
        return root, ancestors

    def find_leaves(self, resource, ancestors=None):
        """Find leaf nodes of the resource hierarchy. These are the storage
        resources containing the actual data."""

        if ancestors is None:
            ancestors = []
        to_visit = [(resource, ancestors)]

        while len(to_visit) > 0:
            node, ancestors = to_visit.pop(0)
            children = node[Resource.children]
            if children:
                ancestors_of_children = ancestors + [node[Resource.name]]
                for child in (c.strip("{}") for c in children.split(";")):
                    child_resource = self.get_resource(child)
                    to_visit.append((child_resource, ancestors_of_children))

            elif node[Resource.location] == self.fqdn:
                print("{} is a storage resource with vault path {}"
                      .format(node[Resource.name], node[Resource.vault_path]),
                      file=sys.stderr)
                hiera = ancestors + [node[Resource.name]]
                yield node, hiera

            else:
                print("Storage resource {} not on fqdn {}, but {}"
                      .format(resource[Resource.name], self.fqdn,
                              resource[Resource.location]),
                      file=sys.stderr)

    def run(self):
        """Must be implemented by subclass"""
        raise NotImplementedError


class ResourceCheck(Check):
    """Starting from a Resource path. Check consistency of database"""

    def __init__(self, session, fqdn, resource_name, root_collection):
        super(ResourceCheck, self).__init__(session, fqdn, root_collection)
        self.resource_name = resource_name

    def run(self):
        print("Checking resource {resource_name} for consistency"
              .format(resource_name=self.resource_name), file=sys.stderr)

        self.formatter.head()

        resource = self.get_resource(self.resource_name)
        root, ancestors = self.find_root(resource)
        self.root = resource
        for leaf, hiera in self.find_leaves(resource, ancestors):
            self.vault = leaf[Resource.vault_path]
            self.hiera = ";".join(hiera)
            self.check_collections()

    def collections_in_root(self):
        """Returns a generator for all the Collections in the root resource"""
        if self.root_collection is None:
            return (self.session.query(Collection.id, Collection.name)
                    .filter(Resource.id == self.root[Resource.id])
                    .get_results()
                    )
        else:
            generator_collection = (self.session.query(Collection.id, Collection.name)
                    .filter(Resource.id == self.root[Resource.id])
                    .filter(Collection.name == self.root_collection)
                    .get_results()
                    )
            generator_subcollections = (self.session.query(Collection.id, Collection.name)
                    .filter(Resource.id == self.root[Resource.id])
                    .filter(Like(Collection.name, self.root_collection + "/%%"))
                    .get_results()
                    )
            return chain(generator_collection, generator_subcollections)

    def data_objects_in_collection(self, coll_id):
        """Returns a generator for all data objects in a collection"""
        return (self.session.query(DataObject)
                .filter(Collection.id == coll_id)
                .filter(DataObject.resc_hier == self.hiera)
                .get_results()
                )

    def check_collections(self):
        """Check every collection within the target resource for consistency"""
        zone_name = self.root[Resource.zone_name]
        prefix = "/" + zone_name

        for coll in self.collections_in_root():
            coll_id = coll[Collection.id]
            coll_name = coll[Collection.name]
            coll_path = coll_name.replace(prefix, self.vault)
            status_on_disk = on_disk(coll_path)
            result = Result(obj_type=ObjectType.COLLECTION,
                            obj_path=coll_name,
                            phy_path=coll_path,
                            status=status_on_disk,
                            observed_values={})
            self.formatter(result)
            if status_on_disk != Status.OK:
                continue

            if PY2:
                print("Checking data objects of collection {} in hierarchy: {}"
                  .format(coll_name.encode('utf-8'), self.hiera),
                  file=sys.stderr)
            else:
                print("Checking data objects of collection {} in hierarchy: {}"
                  .format(coll_name, self.hiera),
                  file=sys.stderr)

            for data_object in self.data_objects_in_collection(coll_id):
                obj_name = data_object[DataObject.name]
                obj_path = coll_name + '/' + obj_name
                phy_path = data_object[DataObject.path]

                object_checker = ObjectChecker(data_object, phy_path)

                status = object_checker.exists_on_disk()
                observed_values = {}
                if status == Status.OK:
                    status, observed_filesizes = object_checker.compare_filesize()
                    observed_values.update(observed_filesizes)
                    if status == Status.OK:
                        status, observed_checksums = object_checker.compare_checksums()
                        observed_values.update(observed_checksums)

                result = Result(ObjectType.DATAOBJECT, obj_path, phy_path, status, observed_values)
                self.formatter(result)


class VaultCheck(Check):
    """Starting from a physical vault path check for consistency"""

    def __init__(self, session, fqdn, vault_path, root_collection):
        super(VaultCheck, self).__init__(session, fqdn, root_collection)
        self.vault_path = vault_path

    def run(self):
        print("Checking vault at {path} for consistency"
              .format(path=self.vault_path),
              file=sys.stderr)

        path = self.vault_path
        storage_resource = self.get_resource_from_phy_path(path)
        while storage_resource is None:
            path = os.path.dirname(path)
            if path == '/':
                sys.exit("Could not find iRODS resource containing {}"
                         .format(self.vault_path))
            storage_resource = self.get_resource_from_phy_path(path)

        self.storage_resource = storage_resource
        self.vault = storage_resource[Resource.vault_path]

        self.root, ancestors = self.find_root(storage_resource)
        hiera = ancestors + [storage_resource[Resource.name]]
        self.hiera = ";".join(hiera)

        if self.root_collection is None:
            path_to_walk = self.vault_path
        else:
            path_to_walk = self.root_collection.replace("/" + self.root[Resource.zone_name], self.vault_path, 1)

        for dirname, subdirs, filenames in os.walk(path_to_walk):

            for subdir in subdirs:
                phy_path = os.path.join(dirname, subdir)
                collection, status = self.get_collection(phy_path)
                if collection:
                    obj_path = collection[Collection.name]
                else:
                    obj_path = "UNKNOWN"
                result = Result(
                    ObjectType.DIRECTORY, obj_path, phy_path, status, {})

                self.formatter(result)

            for filename in filenames:
                phy_path = os.path.join(dirname, filename)
                data_object, status = self.get_data_object(phy_path)
                observed_values = {}
                if data_object is None:
                    obj_path = "UNKNOWN"
                else:
                    object_checker = ObjectChecker(data_object, phy_path)
                    status, observed_filesizes = object_checker.compare_filesize()
                    observed_values.update(observed_filesizes)
                    if status == Status.OK:
                        status, observed_checksums = object_checker.compare_checksums()
                        observed_values.update(observed_checksums)

                    if PY2:
                        obj_path = "{}/{}".format(
                            data_object[Collection.name].encode('utf-8'),
                            data_object[DataObject.name].encode('utf-8')
                            )
                    else:
                        obj_path = "{}/{}".format(
                            data_object[Collection.name],
                            data_object[DataObject.name]
                            )

                result = Result(ObjectType.FILE, obj_path, phy_path, status, observed_values)

                self.formatter(result)

    def get_collection(self, phy_path):
        resource_id = self.storage_resource[Resource.id]
        vault_path = self.storage_resource[Resource.vault_path]
        zone = self.root[Resource.zone_name]
        prefix = '/' + zone
        coll_name = phy_path.replace(vault_path, prefix)
        try:
            collection = (
                self.session.query(Collection, Resource.id)
                .filter(Collection.name == coll_name)
                .filter(Resource.id == resource_id)
                .one()
		        )
        except iexc.NoResultFound:
            collection = None
            status = Status.NOT_REGISTERED
        else:
            status = Status.OK

        return collection, status

    def get_data_object(self, phy_path):
        try:
            result = (
                self.session.query(DataObject, Collection.name)
                .filter(DataObject.path == phy_path)
                .filter(DataObject.resc_hier == self.hiera)
                .first()
                )
        except iexc.NoResultFound:
            status = Status.NOT_REGISTERED
            data_object = None
        else:
            data_object = result
            if data_object is None:
                status = Status.NOT_REGISTERED
            else:
                status = Status.OK

        return data_object, status
