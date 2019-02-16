import collections
import io
import os
import re
import shutil
import subprocess
import tempfile
from xml.etree import ElementTree

import pytest

from pytest_cpp import junit_xml
from pytest_cpp.error import CppTestFailure


class BoostTestFacade(object):
    """
    Facade for BoostTests.
    """

    _JUNIT_LOG_FORMAT = re.compile(r'^.*--log_format=<.*JUNIT.*>.*?$',
                                   re.MULTILINE)
    _LINE_ENDING = re.compile('^.*$', re.MULTILINE)
    _TEST_CASE_PATTERN = re.compile(r'(?P<indent>\s*)(?P<name>[^*\s]+)\*')

    class Facade(object):
        def __init__(self, list_tests_fn, run_test_fn):
            self._list_test_fn = list_tests_fn
            self._run_test_fn = run_test_fn

        def list_tests(self, executable):
            return self._list_test_fn(executable)

        def run_test(self, executable, test_id, test_args=()):
            return self._run_test_fn(executable, test_id, test_args)

    @classmethod
    def is_test_suite(cls, executable):
        try:
            output = subprocess.check_output([executable, '--help'],
                                             stderr=subprocess.STDOUT,
                                             universal_newlines=True)
        except (subprocess.CalledProcessError, OSError):
            return False
        else:
            return '--output_format' in output and 'log_format' in output

    @classmethod
    def create_facade(cls, executable):
        try:
            output = subprocess.check_output([executable, '--help'],
                                             stderr=subprocess.STDOUT,
                                             universal_newlines=True)

            list_test_fn = BoostTestFacade._list_tests_fallback
            # if '--list_content' in output:
            #     list_test_fn = BoostTestFacade._list_tests

            run_test_fn = BoostTestFacade._run_tests_fallback
            if BoostTestFacade._JUNIT_LOG_FORMAT.search(output):
                run_test_fn = BoostTestFacade._run_tests

            return BoostTestFacade.Facade(list_test_fn, run_test_fn)
        except subprocess.CalledProcessError:
            return BoostTestFacade.Facade(BoostTestFacade._list_tests_fallback,
                                          BoostTestFacade._run_tests_fallback)

    def run_test(self, executable, test_id, test_args=()):
        try:
            output = subprocess.check_output([executable, '--help'],
                                             stderr=subprocess.STDOUT,
                                             universal_newlines=True)

            if BoostTestFacade._JUNIT_LOG_FORMAT.search(output):
                return BoostTestFacade._run_tests(executable,
                                                  test_id,
                                                  test_args)
            else:
                return BoostTestFacade._run_tests_fallback(executable,
                                                           test_id,
                                                           test_args)
        except subprocess.CalledProcessError:
            return BoostTestFacade._run_tests_fallback(executable,
                                                       test_id,
                                                       test_args)

    def list_tests(self, executable):
        #TODO: use newer function for newer Boost
        return BoostTestFacade._list_tests_fallback(executable)

    @staticmethod
    def _list_tests_fallback(executable):
        # unfortunately boost doesn't provide us with a way to list the tests
        # inside the executable, so the test_id is a dummy placeholder :(
        return [os.path.basename(os.path.splitext(executable)[0])]

    @staticmethod
    def _list_tests(executable):
        output = subprocess.check_output([executable, '--list_content=HRF'],
                                         stderr=subprocess.STDOUT,
                                         universal_newlines=True)
        prev_indent = 0
        tests = []
        test_names_branch = []

        TestNode = collections.namedtuple('TestNode', ['name', 'indent'])

        def get_lines_iter(text):
            return (l.group(0)
                    for l in BoostTestFacade._LINE_ENDING.finditer(text))

        def get_test_name():
            return '/'.join(map(lambda t: t.name, test_names_branch))

        for line in get_lines_iter(output):
            m = BoostTestFacade._TEST_CASE_PATTERN.match(line)
            if not m:
                continue

            indent = len(m.group('indent'))
            test_name = m.group('name')
            test_node = TestNode(test_name, indent)

            if indent <= prev_indent:
                full_test = get_test_name()
                if full_test:
                    tests.append(full_test)

                while test_names_branch \
                        and test_names_branch[-1].indent >= indent:
                    test_names_branch.pop()

            test_names_branch.append(test_node)
            prev_indent = indent

        if test_names_branch:
            tests.append(get_test_name())
        return tests

    @staticmethod
    def _run_tests_fallback(executable, test_id, test_args=()):
        def read_file(name):
            try:
                with io.open(name) as f:
                    return f.read()
            except IOError:
                return None

        temp_dir = tempfile.mkdtemp()
        try:
            log_xml = os.path.join(temp_dir, 'log.xml')
            report_xml = os.path.join(temp_dir, 'report.xml')
            args = [
                executable,
                '--output_format=XML',
                '--log_sink=%s' % log_xml,
                '--report_sink=%s' % report_xml,
            ]
            args.extend(test_args)
            p = subprocess.Popen(args,
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT)
            stdout, _ = p.communicate()

            log = read_file(log_xml)
            report = read_file(report_xml)

            if p.returncode not in (0, 200, 201):
                msg = ('Internal Error: calling {executable} '
                       'for test {test_id} failed (returncode={returncode}):\n'
                       'output:{stdout}\n'
                       'log:{log}\n'
                       'report:{report}')
                failure = BoostTestFailure(
                    '<no source file>',
                    linenum=0,
                    contents=msg.format(executable=executable,
                                        test_id=test_id,
                                        stdout=stdout,
                                        log=log,
                                        report=report,
                                        returncode=p.returncode))
                return [failure]

            if report is not None and (
                    report.startswith('Boost.Test framework internal error: ') or
                    report.startswith('Test setup error: ')):
                # boost.test doesn't do XML output on fatal-enough errors.
                failure = BoostTestFailure('unknown location', 0, report)
                return [failure]

            results = BoostTestFacade._parse_log(log=log)
            if results:
                return results
        finally:
            shutil.rmtree(temp_dir)

    @staticmethod
    def _run_tests(executable, test_id, test_args=()):
        _, log_xml = tempfile.mkstemp(suffix='log.xml')
        try:
            args = [
                executable,
                '--log_format=JUNIT',
                '--log_sink=%s' % log_xml,
            ]
            args.extend(test_args)
            p = subprocess.Popen(args,
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT)
            stdout, _ = p.communicate()

            results = junit_xml.get_failures(log_xml)
            for (executed_test_id, failures, skipped) in results:
                if executed_test_id == test_id:
                    if failures:
                        return [BoostTestFailure(x) for x in failures]
                    elif skipped:
                        pytest.skip()
                    else:
                        return None
            if results:
                return results
        finally:
            os.remove(log_xml)

    @staticmethod
    def _parse_log(log):
        """
        Parse the "log" section produced by BoostTest.

        This is always a XML file, and from this we produce most of the
        failures possible when running BoostTest.
        """
        # Fatal errors apparently generate invalid xml in the form:
        # <FatalError>...</FatalError><TestLog>...</TestLog>
        # so we have to manually split it into two xmls if that's the case.
        parsed_elements = []
        if log.startswith('<FatalError'):
            fatal, log = log.split('</FatalError>')
            fatal += '</FatalError>'  # put it back, removed by split()
            fatal_root = ElementTree.fromstring(fatal)
            fatal_root.text = 'Fatal Error: %s' % fatal_root.text
            parsed_elements.append(fatal_root)

        log_root = ElementTree.fromstring(log)
        parsed_elements.extend(log_root.findall('Exception'))
        parsed_elements.extend(log_root.findall('Error'))
        parsed_elements.extend(log_root.findall('FatalError'))

        result = []
        for elem in parsed_elements:
            filename = elem.attrib['file']
            linenum = int(elem.attrib['line'])
            result.append(BoostTestFailure(filename, linenum, elem.text))
        return result


class BoostTestFailure(CppTestFailure):
    def __init__(self, filename, linenum, contents):
        self.filename = filename
        self.linenum = linenum
        self.lines = contents.splitlines()

    def get_lines(self):
        m = ('red', 'bold')
        return [(x, m) for x in self.lines]

    def get_file_reference(self):
        return self.filename, self.linenum
