#!/usr/bin/env python

# pylint: disable=locally-disabled, C0111, line-too-long

import argparse
import os
import subprocess
import sys
import threading
from collections import OrderedDict, deque

import yaml
import yodl

DEBUG = False

class DockerCompose(object):
    def __init__(self, compose, project, compose_base_dir, requested_services):
        self.project = project
        self.compose_base_dir = compose_base_dir
        self.services = self.merge_services(compose.get('services', {}))
        self.networks = compose.get('networks', {})
        self.volumes = compose.get('volumes', {})
        self.filtered_services = [service for service in self.services if not requested_services or service in requested_services]

    def project_prefix(self, value):
        return '{}_{}'.format(self.project, value) if self.project else value

    def merge_services(self, services):
        result = OrderedDict()

        for service in services:
            service_config = services[service]
            result[service] = service_config

            if 'extends' in service_config:
                extended_config = service_config['extends']
                extended_service = extended_config['service']

                del result[service]['extends']

                if 'file' in extended_config:
                    extended_service_data = self.merge_services(
                        yaml.load(open(self.compose_base_dir + extended_config['file'], 'r'), yodl.OrderedDictYAMLLoader)['services']
                    )[extended_service]
                else:
                    extended_service_data = result[extended_service]

                merge(result[service], extended_service_data, None, self.merge_env)

        return result

    @staticmethod
    def merge_env(obj1, obj2, key):
        if key == 'environment':
            if isinstance(obj1[key], dict) and isinstance(obj2[key], list):
                obj1[key] = obj2[key] + list({'{}={}'.format(k, v) for k, v in obj1[key].items()})
            elif isinstance(obj1[key], list) and isinstance(obj2[key], dict):
                obj1[key][:0] = list({'{}={}'.format(k, v) for k, v in obj2[key].items()})
            else:
                raise 'Unknown type of "{}" value (should be either list or dictionary)'.format(key)

    @staticmethod
    def call(cmd, ignore_return_code=False):
        print 'Running: \n' + cmd + '\n'
        if not DEBUG:
            proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            returncode = proc.wait()
            stdout = proc.communicate()[0]
            if returncode != 0 and not ignore_return_code:
                print >> sys.stderr, ('Error: command "{}" failed: {}'.format(cmd, stdout))
                sys.exit(returncode)
            else:
                return stdout

    def is_service_exists(self, service):
        return self.call('/bin/bash -o pipefail -c "docker service ls | awk \'{{print \\$2}}\' | (egrep \'^{}$\' || :)"'.format(self.project_prefix(service)))

    def is_external_network(self, network):
        if network not in self.networks:
            print >> sys.stderr, ('Error: network "{}" is not defined in networks'.format(network))
            sys.exit(1)
        return isinstance(self.networks[network], dict) and 'external' in self.networks[network]

    def create_networks(self):
        for network in self.networks:
            if not self.is_external_network(network):
                cmd = '[ "`docker network ls | awk \'{{print $2}}\' | egrep \'^{0}$\'`" != "" ] || docker network create --driver overlay --opt encrypted {0}' \
                    .format(self.project_prefix(network))
                self.call(cmd)

    def create_volumes(self):
        for volume in self.volumes:
            cmd = '[ "`docker volume ls | awk \'{{print $2}}\' | egrep \'^{0}$\'`" != "" ] || docker volume create --name {0}' \
                .format(self.project_prefix(volume))
            if isinstance(self.volumes[volume], dict) and self.volumes[volume]['driver']:
                cmd = cmd + ' --driver={0}'.format(self.volumes[volume]['driver'])
            for opt in self.volumes[volume]['driver_opts']:
                cmd = cmd + ' \\\n --opt {}={}'.format(opt, self.volumes[volume]['driver_opts'][opt])
            self.call(cmd)

    def service_create(self, service):
        service_config = self.services[service]
        cmd = ['docker service create --with-registry-auth \\\n --name', self.project_prefix(service), '\\\n']

        service_image = []
        service_command = []

        def add_flag(key, value=None):
            if value is None:
                cmd.extend([key, '\\\n'])
            elif isinstance(value, int):
                cmd.extend([key, str(value), '\\\n'])
            else:
                cmd.extend([key, shellquote(value), '\\\n'])

        value = ''

        for parameter in service_config:
            value = service_config[parameter]

            def restart():  # pylint: disable=unused-variable
                add_flag('--restart-condition', {'always': 'any'}[value])

            def logging():  # pylint: disable=unused-variable
                add_flag('--log-driver', value.get('driver', 'json-file'))
                log_opts = value['options']
                if log_opts:
                    for key, item in log_opts.items():
                        if item is not None:
                            add_flag('--log-opt', '{}={}'.format(key, item))

            def mem_limit():  # pylint: disable=unused-variable
                add_flag('--limit-memory', value)

            def image():  # pylint: disable=unused-variable
                service_image.append(value)

            def command():  # pylint: disable=unused-variable
                if isinstance(value, list):
                    service_command.extend(value)
                else:
                    service_command.extend(value.split(' '))

            def expose():  # pylint: disable=unused-variable
                pass  # unsupported

            def container_name():  # pylint: disable=unused-variable
                pass  # unsupported

            def hostname():  # pylint: disable=unused-variable
                add_flag('--hostname', value)

            #   --health-cmd string                Command to run to check health
            #   --health-interval duration         Time between running the check (ns|us|ms|s|m|h)
            #   --health-retries int               Consecutive failures needed to report unhealthy
            #   --health-timeout duration          Maximum time to allow one check to run (ns|us|ms|s|m|h)
            #   --no-healthcheck                   Disable any container-specified HEALTHCHECK
            def healthcheck():  # pylint: disable=unused-variable
                if 'disable' in value and value['disable']:
                    add_flag('--no-healthcheck')
                    return
                if 'test' in value:
                    test = deque(value['test'])
                    test_type = test.popleft()
                    print test
                    print test_type
                    if test:
                        if test_type == 'NONE':
                            add_flag('--no-healthcheck')
                        if test_type == 'CMD':
                            add_flag('--healthcheck-cmd', ' '.join(test))
                        if test_type == 'CMD-SHELL':
                            add_flag('--healthcheck-cmd', ' '.join(test))
                if 'interval' in value:
                    add_flag('--health-interval', value['interval'])
                if 'retries' in value:
                    add_flag('--health-retries', value['retries'])
                if 'timeout' in value:
                    add_flag('--health-timeout', value['timeout'])

            def labels():  # pylint: disable=unused-variable
                value = service_config[parameter]
                # ^ working-around the lack of `nonlocal` statement.
                if isinstance(value, dict):
                    value = ('%s=%s' % i for i in value.iteritems())

                for label in value:
                    add_flag('--label', label)

            # --mode string                      Service mode (replicated or global) (default "replicated")
            # --replicas uint                    Number of tasks
            # --constraint list                  Placement constraints (default [])
            # --restart-condition string         Restart when condition is met (none, on-failure, or any)
            # --restart-delay duration           Delay between restart attempts (ns|us|ms|s|m|h)
            # --restart-max-attempts uint        Maximum number of restarts before giving up
            # --restart-window duration          Window used to evaluate the restart policy (ns|us|ms|s|m|h)
            # --update-delay duration            Delay between updates (ns|us|ms|s|m|h) (default 0s)
            # --update-failure-action string     Action on update failure (pause|continue) (default "pause")
            # --update-max-failure-ratio float   Failure rate to tolerate during an update
            # --update-monitor duration          Duration after each task update to monitor for failure (ns|us|ms|s|m|h) (default 0s)
            # --update-parallelism uint          Maximum number of tasks updated simultaneously (0 to update all at once) (default 1)
            def deploy():  # pylint: disable=unused-variable
                if 'mode' in value:
                    add_flag('--mode', value['mode'])
                if 'replicas' in value:
                    add_flag('--replicas', value['replicas'])
                if 'placement' in value and 'constraints' in value['placement']:
                    constraints = value['placement']['constraints']
                    for constraint in constraints:
                        add_flag('--constraint', constraint)
                if 'restart_policy' in value:
                    restart_policy = value['restart_policy']
                    if 'condition' in restart_policy:
                        add_flag('--restart-condition', restart_policy['condition'])
                    if 'delay' in restart_policy:
                        add_flag('--restart-delay', restart_policy['delay'])
                    if 'max_attempts' in restart_policy:
                        add_flag('--restart-max-attempts', restart_policy['max_attempts'])
                    if 'window' in restart_policy:
                        add_flag('--restart-window', restart_policy['window'])
                if 'update_config' in value:
                    update_config = value['update_config']
                    if 'delay' in update_config:
                        add_flag('--update-delay', update_config['delay'])
                    if 'failure_action' in update_config:
                        add_flag('--update-failure-action', update_config['failure_action'])
                    if 'max_failure_ratio' in update_config:
                        add_flag('--update-max-failure-ratio', update_config['max_failure_ratio'])
                    if 'monitor' in update_config:
                        add_flag('--update-monitor', update_config['monitor'])
                    if 'parallelism' in update_config:
                        add_flag('--update-parallelism', update_config['parallelism'])

            def extra_hosts():  # pylint: disable=unused-variable
                pass  # unsupported

            def ports():  # pylint: disable=unused-variable
                for port in value:
                    add_flag('--publish', port)

            def networks():  # pylint: disable=unused-variable
                for network in value:
                    add_flag('--network', network if self.is_external_network(network) else self.project_prefix(network))

            def volumes():  # pylint: disable=unused-variable
                for volume in value:
                    splitted_volume = volume.split(':')
                    src = splitted_volume.pop(0)
                    dst = splitted_volume.pop(0)
                    readonly = 0
                    if splitted_volume and splitted_volume[0] == 'ro':
                        readonly = 1
                    if src.startswith('.'):
                        src = src.replace('.', self.compose_base_dir, 1)

                    if src.startswith('/'):
                        add_flag('--mount', 'type=bind,src={},dst={},readonly={}'.format(src, dst, readonly))
                    else:
                        add_flag('--mount', 'src={},dst={},readonly={}'.format(self.project_prefix(src), dst, readonly))

            def environment():  # pylint: disable=unused-variable
                if isinstance(value, dict):
                    for key, item in value.items():
                        add_flag('--env', '{}={}'.format(key, item))
                else:
                    for env in value:
                        if env.startswith('constraint') or env.startswith('affinity'):
                            constraint = env.split(':', 2)[1]
                            add_flag('--constraint', constraint)
                        else:
                            add_flag('--env', env)

            def env_file():  # pylint: disable=unused-variable
                for item in value:
                    with open(item) as env_file:
                        for line in env_file:
                            if not line.startswith('#') and line.strip():
                                add_flag('--env', line.strip())


            def unsupported():
                print >> sys.stderr, ('WARNING: unsupported parameter {}'.format(parameter))

            locals().get(parameter, unsupported)()

        if not service_image:
            print 'ERROR: no image specified for %s service' % service
            sys.exit(1)

        cmd.extend(service_image)
        cmd.extend(service_command)

        self.call(' '.join(cmd))

    def service_up(self):
        self.create_networks()
        self.create_volumes()

        services_to_start = []

        for service in self.filtered_services:
            if self.is_service_exists(service):
                services_to_start.append(service)
                continue

            self.service_create(service)

        if services_to_start:
            self.service_start(services_to_start)

    def pull(self):
        nodes = self.call("docker node ls | grep Ready | awk -F'[[:space:]][[:space:]]+' '{print $2}'").rstrip().split('\n')

        threads = []

        for node in nodes:
            cmd = '; '.join(['docker -H tcp://{}:2375 pull {}'.format(node, self.services[service]['image']) for service in self.filtered_services])
            threads.append((node, threading.Thread(target=self.call, args=(cmd,))))

        for node, thread in threads:
            print 'Pulling on node {}'.format(node)
            thread.start()

        for node, thread in threads:
            thread.join()
            print 'Node {} - DONE'.format(node)

    def service_stop(self):
        services = filter(self.is_service_exists, self.filtered_services)
        cmd_args = ['{}={}'.format(self.project_prefix(service), 0) for service in services]
        if cmd_args:
            self.call('docker service scale ' + ' '.join(cmd_args))

    def service_remove(self):
        services = filter(self.is_service_exists, self.filtered_services)
        cmd_args = [self.project_prefix(service) for service in services]
        if cmd_args:
            self.call('docker service rm ' + ' '.join(cmd_args))

    def service_start(self, services=None):
        if services is None:
            services = self.filtered_services

        cmd = 'docker service scale ' + \
              ' '.join(['{}={}'.format(self.project_prefix(service), self.services[service].get('replicas', '1')) for service in services])
        self.call(cmd)

def main():
    envs = {
        'COMPOSE_FILE': 'docker-compose.yml',
        'COMPOSE_HTTP_TIMEOUT': '60',
        'COMPOSE_TLS_VERSION': 'TLSv1'
    }
    env_path = os.path.join(os.getcwd(), '.env')

    if os.path.isfile(env_path):
        with open(env_path) as env_file:
            envs.update(dict(map(lambda line: line.strip().split('=', 1), (line for line in env_file if not line.startswith('#') and line.strip()))))

    map(lambda e: os.environ.update({e[0]: e[1]}), (e for e in envs.items() if not e[0] in os.environ))

    parser = argparse.ArgumentParser(formatter_class=lambda prog: argparse.HelpFormatter(prog, max_help_position=50, width=120))
    parser.add_argument('-f', '--file', type=argparse.FileType(), help='Specify an alternate compose file (default: docker-compose.yml)', default=[],
                        action='append')
    parser.add_argument('-p', '--project-name', help='Specify an alternate project name (default: directory name)',
                        default=os.environ.get('COMPOSE_PROJECT_NAME'))
    parser.add_argument('--dry-run', action='store_true')
    subparsers = parser.add_subparsers(title='Command')
    parser.add_argument('_service', metavar='service', nargs='*', help='List of services to run the command for')

    services_parser = argparse.ArgumentParser(add_help=False)
    services_parser.add_argument('service', nargs='*', help='List of services to run the command for')

    pull_parser = subparsers.add_parser('pull', help='Pull service images', add_help=False, parents=[services_parser])
    pull_parser.set_defaults(command='pull')

    rm_parser = subparsers.add_parser('rm', help='Stop and remove services', add_help=False, parents=[services_parser])
    rm_parser.set_defaults(command='service_remove')
    rm_parser.add_argument('-f', help='docker-compose compatibility; ignored', action='store_true')

    start_parser = subparsers.add_parser('start', help='Start services', add_help=False, parents=[services_parser])
    start_parser.set_defaults(command='service_start')

    stop_parser = subparsers.add_parser('stop', help='Stop services', add_help=False, parents=[services_parser])
    stop_parser.set_defaults(command='service_stop')

    up_parser = subparsers.add_parser('up', help='Create and start services', add_help=False, parents=[services_parser])
    up_parser.set_defaults(command='service_up')
    up_parser.add_argument('-d', help='docker-compose compatibility; ignored', action='store_true')

    args = parser.parse_args(sys.argv[1:])

    if not args.file:
        try:
            args.file = map(lambda f: open(f), os.environ['COMPOSE_FILE'].split(':'))
        except IOError as e:
            print e
            parser.print_help()
            sys.exit(1)

    global DEBUG
    DEBUG = args.dry_run

    compose_base_dir = os.path.dirname(os.path.abspath(args.file[0].name))

    if args.project_name is None:
        args.project_name = os.path.basename(compose_base_dir)

    # Decode and merge the compose files
    compose_dicts = map(lambda f: yaml.load(f, yodl.OrderedDictYAMLLoader), args.file)
    merged_compose = reduce(merge, compose_dicts)

    docker_compose = DockerCompose(merged_compose, args.project_name, compose_base_dir + '/', args.service)
    getattr(docker_compose, args.command)()


# Based on http://stackoverflow.com/questions/7204805/dictionaries-of-dictionaries-merge/7205107#7205107
def merge(a, b, path=None, conflict_resolver=None):
    """merges b into a"""
    if path is None:
        path = []
    for key in b:
        if key in a:
            if isinstance(a[key], dict) and isinstance(b[key], dict):
                merge(a[key], b[key], path + [str(key)], conflict_resolver)
            elif isinstance(a[key], list) and isinstance(b[key], list):
                a[key].extend(b[key])
            elif a[key] == b[key]:
                pass  # same leaf value
            else:
                if conflict_resolver:
                    conflict_resolver(a, b, key)
                else:
                    raise Exception('Conflict at %s' % '.'.join(path + [str(key)]))
        else:
            a[key] = b[key]
    return a

def shellquote(s):
    return "'" + s.replace("'", "'\\''") + "'"

if __name__ == "__main__":
    main()
