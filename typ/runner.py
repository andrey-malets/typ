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

import fnmatch
import importlib
import inspect
import json
import pdb
import unittest

from collections import OrderedDict

from typ import json_results
from typ.arg_parser import ArgumentParser
from typ.host import Host
from typ.pool import make_pool
from typ.stats import Stats
from typ.printer import Printer
from typ.test_case import TestCase as TypTestCase
from typ.version import VERSION


Result = json_results.Result
ResultSet = json_results.ResultSet
ResultType = json_results.ResultType


class TestInput(object):

    def __init__(self, name, msg='', timeout=None, expected=None):
        self.name = name
        self.msg = msg
        self.timeout = timeout
        self.expected = expected


class TestSet(object):

    def __init__(self, parallel_tests=None, isolated_tests=None,
                 tests_to_skip=None, context=None, setup_fn=None,
                 teardown_fn=None):

        def promote(tests):
            tests = tests or []
            return [test if isinstance(test, TestInput) else TestInput(test)
                    for test in tests]

        self.parallel_tests = promote(parallel_tests)
        self.isolated_tests = promote(isolated_tests)
        self.tests_to_skip = promote(tests_to_skip)
        self.context = context
        self.setup_fn = setup_fn
        self.teardown_fn = teardown_fn


class _AddTestsError(Exception):
    pass


class Runner(object):

    def __init__(self, host=None, loader=None):
        self.host = host or Host()
        self.loader = loader or unittest.loader.TestLoader()
        self.printer = None
        self.stats = None
        self.cov = None
        self.coverage_source = None
        self.top_level_dir = None
        self.args = None

        # initialize self.args to the defaults.
        parser = ArgumentParser(self.host)
        self.parse_args(parser, [])

    def main(self, argv=None):
        parser = ArgumentParser(self.host)
        self.parse_args(parser, argv)
        if parser.exit_status is not None:
            return parser.exit_status

        try:
            ret, _, _ = self.run()
            return ret
        except KeyboardInterrupt:
            self.print_("interrupted, exiting", stream=self.host.stderr)
            return 130

    def parse_args(self, parser, argv):
        self.args = parser.parse_args(args=argv)
        if parser.exit_status is not None:
            return

    def print_(self, msg='', end='\n', stream=None):
        self.host.print_(msg, end, stream=stream)

    def run(self, test_set=None, classifier=None, context=None,
            setup_fn=None, teardown_fn=None):
        ret = 0
        h = self.host

        if self.args.version:
            self.print_(VERSION)
            return ret, None, None

        ret = self._set_up_runner()
        if ret:  # pragma: no cover
            return ret, None, None

        find_start = h.time()
        if self.cov:  # pragma: no cover
            self.cov.erase()
            self.cov.start()

        full_results = None
        result_set = ResultSet()

        if not test_set:
            ret, test_set = self.find_tests(self.args, classifier, context,
                                            setup_fn, teardown_fn)
        find_end = h.time()

        if not ret:
            ret, full_results = self._run_tests(result_set, test_set)

        if self.cov:  # pragma: no cover
            self.cov.stop()
            self.cov.save()
        test_end = h.time()

        trace = self._trace_from_results(result_set)
        if full_results:
            self._summarize(full_results)
            self.write_results(full_results)
            upload_ret = self.upload_results(full_results)
            if not ret:
                ret = upload_ret
            reporting_end = h.time()
            self._add_trace_event(trace, 'run', find_start, reporting_end)
            self._add_trace_event(trace, 'discovery', find_start, find_end)
            self._add_trace_event(trace, 'testing', find_end, test_end)
            self._add_trace_event(trace, 'reporting', test_end, reporting_end)
            self.write_trace(trace)
            self.report_coverage()
        else:
            upload_ret = 0

        return ret, full_results, trace

    def _set_up_runner(self):
        h = self.host
        args = self.args

        self.stats = Stats(args.status_format, h.time, args.jobs)
        self.printer = Printer(
            self.print_, args.overwrite, args.terminal_width)

        self.top_level_dir = args.top_level_dir
        if not self.top_level_dir:
            if args.tests and h.exists(args.tests[0]):
                # TODO: figure out what to do if multiple files are
                # specified and they don't all have the same correct
                # top level dir.
                top_dir = h.dirname(args.tests[0])
            else:
                top_dir = h.getcwd()
            while h.exists(top_dir, '__init__.py'):
                top_dir = h.dirname(top_dir)
            self.top_level_dir = h.abspath(top_dir)

        h.add_to_path(self.top_level_dir)

        for path in args.path:
            h.add_to_path(path)

        if args.coverage:  # pragma: no cover
            try:
                import coverage
            except ImportError:
                h.print_("Error: coverage is not installed")
                return 1
            source = self.args.coverage_source
            if not source:
                source = [self.top_level_dir] + self.args.path
            self.coverage_source = source
            self.cov = coverage.coverage(source=self.coverage_source,
                                         data_suffix=True)
            self.cov.erase()
        return 0

    def find_tests(self, args, classifier=None,
                   context=None, setup_fn=None, teardown_fn=None):
        test_set = self._make_test_set(context=context,
                                       setup_fn=setup_fn,
                                       teardown_fn=teardown_fn)

        names = self._name_list_from_args(args)
        classifier = classifier or _default_classifier(args)

        for name in names:
            try:
                self._add_tests_to_set(test_set, args.suffixes,
                                       self.top_level_dir, classifier, name)
            except (AttributeError, ImportError, SyntaxError) as e:
                self.print_('Failed to load "%s": %s' % (name, e))
                return 1, None
            except _AddTestsError as e:
                self.print_(str(e))
                return 1, None

        # TODO: Add support for discovering setupProcess/teardownProcess?

        test_set.parallel_tests = _sort_inputs(test_set.parallel_tests)
        test_set.isolated_tests = _sort_inputs(test_set.isolated_tests)
        test_set.tests_to_skip = _sort_inputs(test_set.tests_to_skip)
        return 0, test_set

    def _name_list_from_args(self, args):
        if args.tests:
            names = args.tests
        elif args.file_list:
            if args.file_list == '-':
                s = self.host.stdin.read()
            else:
                s = self.host.read_text_file(args.file_list)
            names = [line.strip() for line in s.splitlines()]
        else:
            names = ['.']
        return names

    def _add_tests_to_set(self, test_set, suffixes, top_level_dir, classifier,
                          name):
        h = self.host
        loader = self.loader
        add_tests = _test_adder(test_set, classifier)

        if h.isfile(name):
            rpath = h.relpath(name, top_level_dir)
            if rpath.endswith('.py'):
                rpath = rpath[:-3]
            module = rpath.replace(h.sep, '.')
            add_tests(loader.loadTestsFromName(module))
        elif h.isdir(name):
            for suffix in suffixes:
                add_tests(loader.discover(name, suffix, top_level_dir))
        else:
            possible_dir = name.replace('.', h.sep)
            if h.isdir(top_level_dir, possible_dir):
                for suffix in suffixes:
                    path = h.join(top_level_dir, possible_dir)
                    suite = loader.discover(path, suffix, top_level_dir)
                    add_tests(suite)
            else:
                add_tests(loader.loadTestsFromName(name))

    def _run_tests(self, result_set, test_set):
        h = self.host
        if not test_set.parallel_tests and not test_set.isolated_tests:
            self.print_('No tests to run.')
            return 1, None

        all_tests = [ti.name for ti in
                     _sort_inputs(test_set.parallel_tests +
                                  test_set.isolated_tests +
                                  test_set.tests_to_skip)]

        if self.args.list_only:
            self.print_('\n'.join(all_tests))
            return 0, None

        self._run_one_set(self.stats, result_set, test_set)

        failed_tests = json_results.failed_test_names(result_set)
        retry_limit = self.args.retry_limit

        while retry_limit and failed_tests:
            if retry_limit == self.args.retry_limit:
                self.flush()
                self.args.overwrite = False
                self.printer.should_overwrite = False
                self.args.verbose = min(self.args.verbose, 1)

            self.print_('')
            self.print_('Retrying failed tests (attempt #%d of %d)...' %
                        (self.args.retry_limit - retry_limit + 1,
                         self.args.retry_limit))
            self.print_('')

            stats = Stats(self.args.status_format, h.time, 1)
            stats.total = len(failed_tests)
            tests_to_retry = self._make_test_set(
                isolated_tests=[TestInput(name) for name in failed_tests],
                context=test_set.context,
                setup_fn=test_set.setup_fn,
                teardown_fn=test_set.teardown_fn)
            retry_set = ResultSet()
            self._run_one_set(stats, retry_set, tests_to_retry)
            result_set.results.extend(retry_set.results)
            failed_tests = json_results.failed_test_names(retry_set)
            retry_limit -= 1

        if retry_limit != self.args.retry_limit:
            self.print_('')

        full_results = json_results.make_full_results(self.args.metadata,
                                                      int(h.time()),
                                                      all_tests, result_set)

        return (json_results.exit_code_from_full_results(full_results),
                full_results)

    def _make_test_set(self, parallel_tests=None, isolated_tests=None,
                       tests_to_skip=None, context=None, setup_fn=None,
                       teardown_fn=None):
        parallel_tests = parallel_tests or []
        isolated_tests = isolated_tests or []
        tests_to_skip = tests_to_skip or []
        return TestSet(_sort_inputs(parallel_tests),
                       _sort_inputs(isolated_tests),
                       _sort_inputs(tests_to_skip),
                       context, setup_fn, teardown_fn)

    def _run_one_set(self, stats, result_set, test_set):
        stats.total = (len(test_set.parallel_tests) +
                       len(test_set.isolated_tests) +
                       len(test_set.tests_to_skip))
        self._skip_tests(stats, result_set, test_set.tests_to_skip)
        self._run_list(stats, result_set, test_set,
                       test_set.parallel_tests, self.args.jobs)
        self._run_list(stats, result_set, test_set,
                       test_set.isolated_tests, 1)

    def _skip_tests(self, stats, result_set, tests_to_skip):
        for test_input in tests_to_skip:
            last = self.host.time()
            stats.started += 1
            self._print_test_started(stats, test_input)
            now = self.host.time()
            result = Result(test_input.name, actual=ResultType.Skip,
                            started=last, took=(now - last), worker=0,
                            expected=[ResultType.Skip],
                            out=test_input.msg)
            result_set.add(result)
            stats.finished += 1
            self._print_test_finished(stats, result)

    def _run_list(self, stats, result_set, test_set, test_inputs, jobs):
        h = self.host
        running_jobs = set()

        jobs = min(len(test_inputs), jobs)
        if not jobs:
            return

        child = _Child(self, self.loader, test_set)
        pool = make_pool(h, jobs, _run_one_test, child,
                         _setup_process, _teardown_process)
        try:
            while test_inputs or running_jobs:
                while test_inputs and (len(running_jobs) < self.args.jobs):
                    test_input = test_inputs.pop(0)
                    stats.started += 1
                    pool.send(test_input)
                    running_jobs.add(test_input.name)
                    self._print_test_started(stats, test_input)

                result = pool.get()
                running_jobs.remove(result.name)
                result_set.add(result)
                stats.finished += 1
                self._print_test_finished(stats, result)
            pool.close()
        finally:
            pool.join()

    def _print_test_started(self, stats, test_input):
        if not self.args.quiet and self.args.overwrite:
            self.update(stats.format() + test_input.name,
                        elide=(not self.args.verbose))

    def _print_test_finished(self, stats, result):
        stats.add_time()

        assert result.actual in [ResultType.Failure, ResultType.Skip,
                                 ResultType.Pass]
        if result.actual == ResultType.Failure:
            result_str = ' failed'
        elif result.actual == ResultType.Skip:
            result_str = ' was skipped'
        elif result.actual == ResultType.Pass:
            result_str = ' passed'

        if result.unexpected:
            result_str += ' unexpectedly'
        if self.args.timing:
            timing_str = ' %.4fs' % result.took
        else:
            timing_str = ''
        suffix = '%s%s' % (result_str, timing_str)
        out = result.out
        err = result.err
        if result.code:
            if out or err:
                suffix += ':\n'
            self.update(stats.format() + result.name + suffix, elide=False)
            for l in out.splitlines():  # pragma: untested
                self.print_('  %s' % l)
            for l in err.splitlines():  # pragma: untested
                self.print_('  %s' % l)
        elif not self.args.quiet:
            if self.args.verbose > 1 and (out or err):  # pragma: untested
                suffix += ':\n'
            self.update(stats.format() + result.name + suffix,
                        elide=(not self.args.verbose))
            if self.args.verbose > 1:  # pragma: untested
                for l in out.splitlines():
                    self.print_('  %s' % l)
                for l in err.splitlines():
                    self.print_('  %s' % l)
            if self.args.verbose:
                self.flush()

    def update(self, msg, elide):
        self.printer.update(msg, elide)

    def flush(self):
        self.printer.flush()

    def _summarize(self, full_results):
        num_tests = self.stats.finished
        num_failures = json_results.num_failures(full_results)

        if self.args.quiet and num_failures == 0:
            return

        if self.args.timing:
            timing_clause = ' in %.1fs' % (self.host.time() -
                                           self.stats.started_time)
        else:
            timing_clause = ''
        self.update('%d test%s run%s, %d failure%s.' %
                    (num_tests,
                     '' if num_tests == 1 else 's',
                     timing_clause,
                     num_failures,
                     '' if num_failures == 1 else 's'), elide=False)
        self.print_()

    def write_trace(self, trace):
        if self.args.write_trace_to:
            self.host.write_text_file(
                self.args.write_trace_to,
                json.dumps(trace, indent=2) + '\n')

    def write_results(self, full_results):
        if self.args.write_full_results_to:
            self.host.write_text_file(
                self.args.write_full_results_to,
                json.dumps(full_results, indent=2) + '\n')

    def upload_results(self, full_results):
        h = self.host
        if not self.args.test_results_server:
            return 0

        url, content_type, data = json_results.make_upload_request(
            self.args.test_results_server, self.args.builder_name,
            self.args.master_name, self.args.test_type,
            full_results)

        try:
            h.fetch(url, data, {'Content-Type': content_type})
            return 0
        except Exception as e:
            h.print_('Uploading the JSON results raised "%s"' % str(e))
            return 1

    def report_coverage(self):
        if self.args.coverage:  # pragma: no cover
            self.host.print_()
            import coverage
            cov = coverage.coverage(data_suffix=True)
            cov.combine()
            cov.report(show_missing=self.args.coverage_show_missing,
                       omit=self.args.coverage_omit)

    def _add_trace_event(self, trace, name, start, end):
        event = {
            'name': name,
            'ts': int((start - self.stats.started_time) * 1000000),
            'dur': int((end - start) * 1000000),
            'ph': 'X',
            'pid': self.host.getpid(),
            'tid': 0,
        }
        trace['traceEvents'].append(event)

    def _trace_from_results(self, result_set):
        trace = OrderedDict()
        trace['traceEvents'] = []
        trace['otherData'] = {}
        for m in self.args.metadata:
            k, v = m.split('=')
            trace['otherData'][k] = v

        for result in result_set.results:
            started = int((result.started - self.stats.started_time) * 1000000)
            took = int(result.took * 1000000)
            event = OrderedDict()
            event['name'] = result.name
            event['dur'] = took
            event['ts'] = started
            event['ph'] = 'X'  # "Complete" events
            event['pid'] = result.pid
            event['tid'] = result.worker

            args = OrderedDict()
            args['expected'] = sorted(str(r) for r in result.expected)
            args['actual'] = str(result.actual)
            args['out'] = result.out
            args['err'] = result.err
            args['code'] = result.code
            args['unexpected'] = result.unexpected
            args['flaky'] = result.flaky
            event['args'] = args

            trace['traceEvents'].append(event)
        return trace


def _matches(name, globs):
    return any(fnmatch.fnmatch(name, glob) for glob in globs)


def _default_classifier(args):
    def default_classifier(test_set, test):
        name = test.id()
        if _matches(name, args.skip):
            test_set.tests_to_skip.append(TestInput(name,
                                                    'skipped by request'))
        elif _matches(name, args.isolate):
            test_set.isolated_tests.append(TestInput(name))
        else:
            test_set.parallel_tests.append(TestInput(name))
    return default_classifier


def _test_adder(test_set, classifier):
    def add_tests(obj):
        if isinstance(obj, unittest.suite.TestSuite):
            for el in obj:
                add_tests(el)
        elif (obj.id().startswith('unittest.loader.LoadTestsFailure') or
              obj.id().startswith('unittest.loader.ModuleImportFailure')
              ):  # pragma: untested
            # Access to protected member pylint: disable=W0212
            module_name = obj._testMethodName
            try:
                method = getattr(obj, obj._testMethodName)
                method()
            except Exception as e:
                if 'LoadTests' in obj.id():
                    raise _AddTestsError('%s.load_tests() failed: %s'
                                         % (module_name, str(e)))
                else:
                    raise _AddTestsError(str(e))
        else:
            assert isinstance(obj, unittest.TestCase)
            classifier(test_set, obj)
    return add_tests


class _Child(object):

    def __init__(self, parent, loader, test_set):
        self.host = None
        self.worker_num = None
        self.debugger = parent.args.debugger
        self.coverage = parent.args.coverage and parent.args.jobs > 1
        self.coverage_source = parent.coverage_source
        self.dry_run = parent.args.dry_run
        self.loader = loader
        self.passthrough = parent.args.passthrough
        self.context = test_set.context
        self.setup_fn = test_set.setup_fn
        self.teardown_fn = test_set.teardown_fn
        self.context_after_setup = None
        self.top_level_dir = parent.top_level_dir
        self.loaded_suites = {}
        self.cov = None


def _setup_process(host, worker_num, child):
    child.host = host
    child.worker_num = worker_num

    if child.coverage:  # pragma: no cover
        import coverage
        child.cov = coverage.coverage(source=child.coverage_source,
                                      data_suffix=True)
        child.cov._warn_no_data = False
        child.cov.start()

    if child.setup_fn:
        child.context_after_setup = child.setup_fn(child, child.context)
    else:
        child.context_after_setup = child.context
    return child


def _teardown_process(child):
    if child.teardown_fn:
        child.teardown_fn(child, child.context_after_setup)
    # TODO: Return a more structured result, including something from
    # the teardown function?

    if child.cov:  # pragma: no cover
        child.cov.stop()
        child.cov.save()

    return child.worker_num


def _run_one_test(child, test_input):
    h = child.host
    pid = h.getpid()
    test_name = test_input.name

    start = h.time()

    # It is important to capture the output before loading the test
    # to ensure that
    # 1) the loader doesn't logs something we don't captured
    # 2) neither the loader nor the test case grab a reference to the
    #    uncaptured stdout or stderr that later is used when the test is run.
    # This comes up when using the FakeTestLoader and testing typ itself,
    # but could come up when testing non-typ code as well.
    h.capture_output(divert=not child.passthrough)

    try:
        suite = child.loader.loadTestsFromName(test_name)
    except Exception as e:
        suite = _load_via_load_tests(child, test_name)

    tests = list(suite)
    if len(tests) != 1:
        err = 'failed to load %s: %s' % (test_name, str(e))
        h.restore_output()
        return Result(test_name, ResultType.Failure, start, 0,
                        child.worker_num, unexpected=True, code=1,
                        err=err, pid=pid)

    test_case = tests[0]
    if isinstance(test_case, TypTestCase):
        test_case.child = child
        test_case.context = child.context_after_setup

    test_result = unittest.TestResult()
    out = ''
    err = ''
    try:
        if child.dry_run:
            pass
        elif child.debugger:  # pragma: no cover
            _run_under_debugger(h, test_case, suite, test_result)
        else:
            suite.run(test_result)
    finally:
        out, err = h.restore_output()

    took = h.time() - start
    return _result_from_test_result(test_result, test_name, start, took, out,
                                    err, child.worker_num, pid)


def _run_under_debugger(host, test_case, suite,
                        test_result):  # pragma: no cover
    # Access to protected member pylint: disable=W0212
    test_func = getattr(test_case, test_case._testMethodName)
    fname = inspect.getsourcefile(test_func)
    lineno = inspect.getsourcelines(test_func)[1] + 1
    dbg = pdb.Pdb(stdout=host.stdout.stream)
    dbg.set_break(fname, lineno)
    dbg.runcall(suite.run, test_result)


def _result_from_test_result(test_result, test_name, start, took, out, err,
                             worker_num, pid):
    flaky = False
    if test_result.failures:
        expected = [ResultType.Pass]
        actual = ResultType.Failure
        code = 1
        unexpected = True
        err = err + test_result.failures[0][1]
    elif test_result.errors:
        expected = [ResultType.Pass]
        actual = ResultType.Failure
        code = 1
        unexpected = True
        err = err + test_result.errors[0][1]
    elif test_result.skipped:
        expected = [ResultType.Skip]
        actual = ResultType.Skip
        err = err + test_result.skipped[0][1]
        code = 0
        unexpected = False
    elif test_result.expectedFailures:
        expected = [ResultType.Failure]
        actual = ResultType.Failure
        code = 1
        err = err + test_result.expectedFailures[0][1]
        unexpected = False
    elif test_result.unexpectedSuccesses:
        expected = [ResultType.Failure]
        actual = ResultType.Pass
        code = 0
        unexpected = True
    else:
        expected = [ResultType.Pass]
        actual = ResultType.Pass
        code = 0
        unexpected = False

    return Result(test_name, actual, start, took, worker_num,
                  expected, unexpected, flaky, code, out, err, pid)


def _load_via_load_tests(child, test_name):
    # If we couldn't import a test directly, the test may be only loadable
    # via unittest's load_tests protocol. See if we can find a load_tests
    # entry point that will work for this test.
    loader = child.loader
    comps = test_name.split('.')
    new_suite = unittest.TestSuite()

    while comps:
        name = '.'.join(comps)
        module = None
        suite = None
        if name not in child.loaded_suites:
            try:
                module = importlib.import_module(name)
            except ImportError:
                pass
            if module:
                suite = loader.loadTestsFromModule(module)
            child.loaded_suites[name] = suite
        suite = child.loaded_suites[name]
        if suite:
            for test_case in suite:
                assert isinstance(test_case, unittest.TestCase)
                if test_case.id() == test_name:
                    new_suite.addTest(test_case)
                    break
        comps.pop()
    return new_suite


def _sort_inputs(inps):
    return sorted(inps, key=lambda inp: inp.name)
