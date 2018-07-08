from xml.etree import ElementTree


def parse(filename):
    root = ElementTree.parse(filename)
    result = []
    for test_suite in root.findall('testsuite'):
        test_suite_name = test_suite.attrib['name']
        for test_case in test_suite.findall('testcase'):
            test_name = test_case.attrib['name']
            failures = []
            failure_elements = test_case.findall('failure')
            for failure_elem in failure_elements:
                failures.append(failure_elem.text)

            # errors = test_case.findall('error')
            # for error in errors:
            #     failures.append()
            skipped = test_case.attrib['status'] == 'notrun'
            result.append(
                (test_suite_name + '.' + test_name, failures, skipped))

    return result
