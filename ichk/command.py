"""Check recursively if an iRods resource is consistent with its vault or
vice versa"""
from __future__ import print_function
import sys
import os
import argparse
import json
from getpass import getpass
from ichk import check
from irods.session import iRODSSession
from irodsutils import password_obfuscation


def entry():
    """Use as entry_point in setup.py"""

    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("-f", "--fqdn",
                        help="FQDN of resource")
    resource_or_vault = parser.add_mutually_exclusive_group(required=True)
    resource_or_vault.add_argument("-r", "--resource",
                                   help="iRODS path of resource")
    resource_or_vault.add_argument("-v", "--vault",
                                   help="Physical path of the resource vault")
    parser.add_argument("-o", "--output", type=argparse.FileType('w'),
                        help="Write output to file")
    parser.add_argument("-m", "--format", dest="fmt", default='human',
                        help="Output format", choices=['human', 'csv'])
    parser.add_argument("-t", "--truncate", default=False,
                        help="Truncate the output to the width of the console")
    parser.add_argument("-T", "--timeout", default=10*60, type=int,
                        help="Sets the maximum amount of seconds to wait for server responses"
                            +", default 600. Increase this to account for longer-running queries.")
    parser.add_argument("-s", "--root-collection", dest='root_collection', default=None,
                        help="Only check a particular collection and its subcollections.")
    parser.add_argument("-a", "--show-size-checksum", default=False, action='store_const', const=True,
                        help="Show expected versus observed object size or checksum in case of differences. "
                            +"Note that checksums are not verified if the observed size of the object is different from the expected size."
                            +"If the output format is 'csv', compared size and checksum values are always printed, "
                            +"irrespective of whether the expected value matches the observed value.")

    args = parser.parse_args()

    if args.fqdn:
        pass
    else:
        import socket
        args.fqdn = socket.getfqdn()

    session = setup_session()

    session.connection_timeout = args.timeout

    run(session, args)


def setup_session():
    """Use irods environment files to configure a iRODSSession"""

    env_json = os.path.expanduser("~/.irods/irods_environment.json")
    try:
        with open(env_json, 'r') as f:
            irods_env = json.load(f)
    except OSError:
        sys.exit("Can not find or access {}. Please use iinit".format(env_json))

    irodsA = os.path.expanduser("~/.irods/.irodsA")
    try:
        with open(irodsA, "r") as r:
            scrambled_password = r.read()
            password = password_obfuscation.decode(scrambled_password)
    except OSError:
        print(
            "Could not open {} .".format(scrambled_password),
            file=sys.stderr
        )
        password = getpass(prompt="Please provide your irods password:")

    print(
        "Connecting to irods at {irods_host}:{irods_port} as {irods_user_name}"
        .format(**irods_env), file=sys.stderr
    )

    session = iRODSSession(
        host=irods_env["irods_host"],
        port=irods_env["irods_port"],
        user=irods_env["irods_user_name"],
        password=password,
        zone=irods_env["irods_zone_name"],
    )

    return session


def run(session, args):
    if args.resource:
        executor = check.ResourceCheck(session, args.fqdn, args.resource, args.root_collection)
    else:
        executor = check.VaultCheck(session, args.fqdn, args.vault, args.root_collection)

    options = {'output': args.output or sys.stdout, 'fmt': args.fmt, 'show_size_checksum': args.show_size_checksum}
    if args.truncate:
        options['truncate'] = True

    executor.setformatter(**options)

    executor.run()
