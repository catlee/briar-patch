#!/usr/bin/env python
"""
Watches pending jobs and starts or creates EC2 instances if required
"""
import re, time

try:
    import simplejson as json
except ImportError:
    import json

import boto.ec2
import sqlalchemy as sa

from aws_create_instance import make_instances

import logging
log = logging.getLogger()

def find_pending(db):
    engine = sa.create_engine(db)
    result = engine.execute(sa.text("""
        SELECT buildername, count(*) FROM
               buildrequests WHERE
               complete=0 AND
               claimed_at=0 AND
               submitted_at > :yesterday

               GROUP BY buildername"""), yesterday=time.time()-86400)
    retval = result.fetchall()
    return retval

def aws_resume_instances(instance_type, count, regions, secrets):
    "resume up to `count` stopped instances of the given type in the given regions"
    started = 0
    for region in regions:
        conn = boto.ec2.connect_to_region(region, **secrets)
        reservations = conn.get_all_instances(filters={
            'tag:moz-type': instance_type,
            'instance-state-name': 'stopped',
            })
        for r in reservations:
            for i in r.instances:
                if not i.tags.get('moz-type') == instance_type:
                    log.debug("skipping %s; wrong type (%s)", i, i.tags.get('moz-type'))
                    continue
                if i.state != 'stopped':
                    log.debug("skipping %s; wrong state (%s)", i, i.state)
                    continue
                log.debug("starting %s...", i)
                i.start()
                started += 1

                if started == count:
                    return started

    return started

def aws_create_instances(instance_type, count, regions, secrets, key_name, instance_data):
    instance_config = json.load(open("configs/%s" % instance_type))
    max_count = instance_config['max_instances']

    # Count how many we have in all regions
    num = 0
    instances = []
    names = []
    for region in regions:
        conn = boto.ec2.connect_to_region(region, **secrets)
        reservations = conn.get_all_instances(filters={'tag:moz-type': instance_type})
        for r in reservations:
            for i in r.instances:
                if i.tags.get('moz-type') == instance_type:
                    instances.append(i)
                    names.append(i.tags['Name'])
                    num += 1

    num_to_create = min(max_count - num, count)
    log.info("We have %i instances across all regions; we will create %i more (max is %i)", num, num_to_create, max_count)

    i = 1
    to_create = []
    while len(to_create) < num_to_create:
        # Figure out its names
        name = instance_config['hostname'] % i
        if name not in names and name not in to_create:
            to_create.append(name)
        i += 1

    log.info("Creating %s", to_create)

    # TODO do multi-region
    if to_create:
        make_instances(to_create, config, regions[0], secrets, key_name, instance_data, create_ami=False)

    return len(to_create)

def aws_watch_pending(db, regions, secrets, key_name, instance_data, builder_map):
    # First find pending jobs in the db
    pending = find_pending(db)

    # Mapping of instance types to # of instances we want to creates
    to_create = {}
    # Then match them to the builder_map
    for pending_buildername, count in pending:
        for buildername_exp, instance_type in builder_map.items():
            if re.match(buildername_exp, pending_buildername):
                log.debug("%s has %i pending jobs, checking instances of type %s", pending_buildername, count, instance_type)
                to_create[instance_type] = to_create.get(instance_type, 0) + count

                break
        else:
            log.debug("%s has %i pending jobs, but no instance types defined", pending_buildername, count)

    for instance_type, count in to_create.items():
        log.debug("Need %i %s", count, instance_type)

        # Check for stopped instances in the given regions and start them if there are any
        started = aws_resume_instances(instance_type, count, regions, secrets)
        count -= started
        log.info("Started %i instances; need %i", started, count)

        # Then create new instances (subject to max_instances)
        created = aws_create_instances(instance_type, count, regions, secrets, key_name, instance_data)
        count -= created
        log.info("Created %i instances; need %i", created, count)

if __name__ == '__main__':
    from optparse import OptionParser
    parser = OptionParser()
    parser.set_defaults(
            regions=[],
            secrets=None,
            loglevel=logging.INFO,
            key_name=None,
            instance_data=None,
            config=None,
            )

    parser.add_option("-r", "--region", action="append", dest="regions")
    parser.add_option("-k", "--secrets", dest="secrets")
    parser.add_option("-s", "--key-name", dest="key_name")
    parser.add_option("-v", "--verbose", action="store_const", dest="loglevel", const=logging.DEBUG)
    parser.add_option("-i", "--instance-data", dest="instance_data")
    parser.add_option("-c", "--config", dest="config")

    options, args = parser.parse_args()

    logging.basicConfig(level=options.loglevel)
    logging.getLogger("boto").setLevel(logging.INFO)

    if not options.regions:
        options.regions = ['us-west-1']

    if not options.secrets:
        parser.error("secrets are required")

    if not options.instance_data:
        parser.error("instance data is required")

    if not options.config:
        parser.error("you must specify a config file to use")

    config = json.load(open(options.config))
    secrets = json.load(open(options.secrets))
    instance_data = json.load(open(options.instance_data))
    aws_watch_pending(config['db'], options.regions, secrets, options.key_name, instance_data, config['buildermap'])
