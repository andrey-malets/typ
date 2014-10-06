# Copyright 2014 Google Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import optparse
import sys

from typ.host import Host


class _Bailout(Exception):
    pass


DEFAULT_COVERAGE_OMIT = ['*/typ/*', '*/site-packages/*']
DEFAULT_STATUS_FORMAT = '[%f/%t] '
DEFAULT_SUFFIXES = ['*_test.py', '*_unittest.py']


class ArgumentParser(argparse.ArgumentParser):

    @staticmethod
    def add_option_group(parser, title, discovery=False,
                         running=False, reporting=False, skip=None):
        # TODO: Get rid of this when telemetry upgrades to argparse.
        ap = ArgumentParser(add_help=False, version=False, discovery=discovery,
                            running=running, reporting=reporting)
        optlist = ap.optparse_options(skip=skip)
        group = optparse.OptionGroup(parser, title)
        group.add_options(optlist)
        parser.add_option_group(group)

    def __init__(self, host=None, add_help=True, version=True, discovery=True,
                 reporting=True, running=True):
        super(ArgumentParser, self).__init__(prog='typ', add_help=add_help)

        self._host = host or Host()
        self.exit_status = None

        self.usage = '%(prog)s [options] [tests...]'

        if version:
            self.add_argument('-V', '--version', action='store_true',
                              help='Print the typ version and exit.')

        if discovery:
            self.add_argument('-f', '--file-list', metavar='FILENAME',
                              action='store',
                              help=('Takes the list of tests from the file '
                                    '(use "-" for stdin).'))
            self.add_argument('--isolate', metavar='glob', default=[],
                              action='append',
                              help=('Globs of tests to run in isolation '
                                    '(serially).'))
            self.add_argument('--skip', metavar='glob', default=[],
                              action='append',
                              help=('Globs of test names to skip (can specify '
                                    'multiple times).'))
            self.add_argument('--suffixes', metavar='glob', default=[],
                              action='append',
                              help=('Globs of test filenames to look for ('
                                    'can specify multiple times; defaults '
                                    'to %s).' % DEFAULT_SUFFIXES))

        if reporting:
            self.add_argument('--builder-name',
                              help=('Builder name to include in the '
                                    'uploaded data.'))
            self.add_argument('-c', '--coverage', action='store_true',
                              help='Reports coverage information.')
            self.add_argument('--coverage-source', action='append',
                              default=[],
                              help=('Directories to include when running and '
                                    'reporting coverage (defaults to '
                                    '--top-level-dir plus --path)'))
            self.add_argument('--coverage-omit', action='append',
                              default=[],
                              help=('Globs to omit when reporting coverage '
                                    '(defaults to %s).' %
                                    DEFAULT_COVERAGE_OMIT))
            self.add_argument('--master-name',
                              help=('Buildbot master name to include in the '
                                    'uploaded data.'))
            self.add_argument('--metadata', action='append', default=[],
                              help=('Optional key=value metadata that will '
                                    'be included in the results.'))
            self.add_argument('--test-results-server',
                              help=('If specified, uploads the full results '
                                    'to this server.'))
            self.add_argument('--test-type',
                              help=('Name of test type to include in the '
                                    'uploaded data (e.g., '
                                    '"telemetry_unittests").'))
            self.add_argument('--write-full-results-to', metavar='FILENAME',
                              action='store',
                              help=('If specified, writes the full results to '
                                    'that path.'))
            self.add_argument('--write-trace-to', metavar='FILENAME',
                              action='store',
                              help=('If specified, writes the trace to '
                                    'that path.'))
            self.add_argument('tests', nargs='*', default=[],
                              help=argparse.SUPPRESS)

        if running:
            self.add_argument('-d', '--debugger', action='store_true',
                              help='Runs the tests under the debugger.')
            self.add_argument('-j', '--jobs', metavar='N', type=int,
                              default=self._host.cpu_count(),
                              help=('Runs N jobs in parallel '
                                    '(defaults to %(default)s).'))
            self.add_argument('-l', '--list-only', action='store_true',
                              help='Lists all the test names found and exits.')
            self.add_argument('-n', '--dry-run', action='store_true',
                              help=argparse.SUPPRESS)
            self.add_argument('-q', '--quiet', action='store_true',
                              default=False,
                              help=('Runs as quietly as possible '
                                    '(only prints errors).'))
            self.add_argument('-s', '--status-format',
                              default=self._host.getenv('NINJA_STATUS',
                                                        DEFAULT_STATUS_FORMAT),
                              help=argparse.SUPPRESS)
            self.add_argument('-t', '--timing', action='store_true',
                              help='Prints timing info.')
            self.add_argument('-v', '--verbose', action='count', default=0,
                              help=('Prints more stuff (can specify multiple '
                                    'times for more output).'))
            self.add_argument('--passthrough', action='store_true',
                              default=False,
                              help='Prints all output while running.')
            self.add_argument('--retry-limit', type=int, default=0,
                              help='Retries each failure up to N times.')
            self.add_argument('--terminal-width', type=int,
                              default=self._host.terminal_width(),
                              help=argparse.SUPPRESS)
            self.add_argument('--overwrite', action='store_true',
                              default=None,
                              help=argparse.SUPPRESS)
            self.add_argument('--no-overwrite', action='store_false',
                              dest='overwrite', default=None,
                              help=argparse.SUPPRESS)
            self.add_argument('--setup', help=argparse.SUPPRESS)
            self.add_argument('--teardown', help=argparse.SUPPRESS)
            self.add_argument('--context', help=argparse.SUPPRESS)

        if discovery or running:
            self.add_argument('-P', '--path', action='append', default=[],
                              help=('Adds dir to sys.path (can specify '
                                    'multiple times).'))
            self.add_argument('--top-level-dir', default=None,
                              help=('Sets the top directory of project '
                                    '(used when running subdirs).'))

    def parse_args(self, args=None, namespace=None):
        try:
            rargs = super(ArgumentParser, self).parse_args(args=args,
                                                           namespace=namespace)
        except _Bailout:
            return None

        for val in rargs.metadata:
            if '=' not in val:
                self._print_message('Error: malformed --metadata "%s"' % val)
                self.exit_status = 2

        if rargs.test_results_server:
            if not rargs.builder_name:
                self._print_message('Error: --builder-name must be specified '
                                    'along with --test-result-server')
                self.exit_status = 2
            if not rargs.master_name:
                self._print_message('Error: --master-name must be specified '
                                    'along with --test-result-server')
                self.exit_status = 2
            if not rargs.test_type:
                self._print_message('Error: --test-type must be specified '
                                    'along with --test-result-server')
                self.exit_status = 2

        if not rargs.suffixes:
            rargs.suffixes = DEFAULT_SUFFIXES

        if not rargs.coverage_omit:
            rargs.coverage_omit = DEFAULT_COVERAGE_OMIT

        if rargs.debugger:  # pragma: no cover
            if sys.version_info.major == 3:
                self._print_message('Error: --debugger does not work w/ '
                                    'Python3 yet.')
                self.exit_status = 2
            rargs.jobs = 1
            rargs.passthrough = True

        if rargs.coverage:  # pragma: no cover
            rargs.jobs = 1

        if rargs.overwrite is None:
            rargs.overwrite = self._host.stdout.isatty() and not rargs.verbose

        return rargs

    # Redefining built-in 'file' pylint: disable=W0622

    def _print_message(self, msg, file=None):
        self._host.print_(msg=msg, stream=file, end='\n')

    def print_help(self, file=None):
        self._print_message(msg=self.format_help(), file=file)

    def error(self, message):
        self.exit(2, '%s: error: %s\n' % (self.prog, message))

    def exit(self, status=0, message=None):
        self.exit_status = status
        if message:
            self._print_message(message, file=self._host.stderr)
        raise _Bailout()

    def optparse_options(self, skip=None):
        skip = skip or []
        options = []
        for action in self._actions:
            args = [flag for flag in action.option_strings if flag not in skip]
            if not args or action.help == '==SUPPRESS==':
                # must either be a positional argument like 'tests'
                # or an option we want to skip altogether.
                continue

            kwargs = {
                'default': action.default,
                'dest': action.dest,
                'help': action.help,
                'metavar': action.metavar,
                'type': action.type,
                'action': _action_str(action)
            }
            options.append(optparse.make_option(*args, **kwargs))
        return options


def _action_str(action):
    # Access to a protected member pylint: disable=W0212
    if isinstance(action, argparse._StoreTrueAction):
        return 'store_true'
    if isinstance(action, argparse._StoreFalseAction):  # pragma: no cover
        return 'store_false'
    if isinstance(action, argparse._StoreAction):
        return 'store'
    if isinstance(action, argparse._CountAction):
        return 'count'
    if isinstance(action, argparse._AppendAction):
        return 'append'
    if isinstance(action, argparse._HelpAction):  # pragma: no cover
        return 'help'

    raise ValueError('Unexpected action type %s for %s' %
                     action.__class__,
                     str(action.option_strings))  # pragma: no cover
