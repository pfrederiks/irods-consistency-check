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


def entry():
    """Use as entry_point in setup.py"""
    env_json = os.path.expanduser("~/.irods/irods_environment.json")
    try:
        with open(env_json, 'r') as f:
            irods_env = json.load(f)
    except OSError:
        sys.exit("Can not find or access {}. Please use iinit".format(env_json))

    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("-f", "--fqdn", default=irods_env["irods_host"],
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

    args = parser.parse_args()

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

    if args.resource:
        executor = check.ResourceCheck(session, args.fqdn, args.resource)
    else:
        executor = check.VaultCheck(session, args.fqdn, args.vault)

    options = { 'output': args.output or sys.stdout, 'fmt': args.fmt}
    if args.truncate:
        options['truncate'] = True

    executor.setformatter(**options)

    executor.run()
