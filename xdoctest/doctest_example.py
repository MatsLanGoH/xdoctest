# -*- coding: utf-8 -*-
"""
This module defines the main class that holds a DocTest example
"""
from __future__ import print_function, division, absolute_import, unicode_literals
import __future__
from collections import OrderedDict
import traceback
import six
import warnings
import math
import sys
import re
from xdoctest import utils
from xdoctest import directive
from xdoctest import constants
from xdoctest import static_analysis as static
from xdoctest import parser
from xdoctest import checker
from xdoctest import exceptions


class Config(dict):
    """
    Static configuration for collection, execution, and reporting doctests.
    Note dynamic directives are not managed by Config, they use RuntimeState.
    """
    def __init__(self, *args, **kwargs):
        super(Config, self).__init__(*args, **kwargs)
        self.update({
            'colored': True,
            # 'colored': False,
            'on_error': 'raise',

            'partnos': False,
            # 'partnos': True,

            # if True formated source linenumbers will agree with their
            # location in the source file. Otherwise they will be relative
            # to the doctest itself.
            'offset_linenos': False,

            'reportchoice': 'udiff',
            'default_runtime_state': {},
            'verbose': 1,
        })

    def getvalue(self, key, given=None):
        if given is None:
            return self[key]
        else:
            return given


class DocTest(object):
    """
    Holds information necessary to execute and verify a doctest

    Attributes:

        num (int): the index of the doctest in the docstring. (i.e.
            this object refers to the num-th doctest within a docstring)

        lineno (int): the line (starting from 1) in the file that the doctest
            begins on. (i.e. if you were to go to this line in the file, the
            first line of the doctest should be on this line).

    Example:
        >>> from xdoctest import core
        >>> from xdoctest import doctest_example
        >>> modpath = doctest_example.__file__
        >>> testables = core.parse_doctestables(modpath)
        >>> for test in testables:
        >>>     if test.callname == 'DocTest':
        >>>         self = test
        >>>         break
        >>> assert self.num == 0
        >>> assert self.modpath == modpath
        >>> print(self)
        <DocTest(xdoctest.doctest_example DocTest:0 ln ...)>
    """

    def __init__(self, docsrc, modpath=None, callname=None, num=0,
                 lineno=1, fpath=None, block_type=None, mode='pytest'):

        # if we know the google block type it is recorded
        self.block_type = block_type

        self.config = Config()

        self.modpath = modpath
        self.fpath = fpath
        if modpath is None:
            self.modname = '<modname?>'
            self.modpath = '<modpath?>'
        else:
            if fpath is not None:
                assert fpath == modpath, (
                    'only specify fpath for non-python files')
            self.fpath = modpath
            self.modname = static.modpath_to_modname(modpath)
        if callname is None:
            self.callname = '<callname?>'
        else:
            self.callname = callname
        self.docsrc = docsrc
        self.lineno = lineno

        self.num = num

        self._parts = None
        self.tb_lineno = None
        self.exc_info = None
        self.failed_part = None
        self.warn_list = None

        self.logged_evals = OrderedDict()
        self.logged_stdout = OrderedDict()
        self._unmatched_stdout = []
        self.skipped_parts = []

        self._runstate = None

        self.module = None
        self.globs = {}
        # Hint at what is running this doctest
        self.mode = mode

    def __nice__(self):
        parts = []
        parts.append(self.modname)
        parts.append('%s:%s' % (self.callname, self.num))
        if self.lineno is not None:
            parts.append('ln %s' % (self.lineno))
        return ' '.join(parts)

    def __repr__(self):
        classname = self.__class__.__name__
        devnice = self.__nice__()
        return '<%s(%s) at %s>' % (classname, devnice, hex(id(self)))

    def __str__(self):
        classname = self.__class__.__name__
        devnice = self.__nice__()
        return '<%s(%s)>' % (classname, devnice)

    def is_disabled(self, pytest=False):
        """
        Checks for comment directives on the first line of the doctest

        A doctest is disabled if it starts with any of the following patterns:
            # DISABLE_DOCTEST
            # SCRIPT
            # UNSTABLE
            # FAILING

        And if running in pytest, you can also use
            # pytest.skip
        """
        disable_patterns = [
            r'>>>\s*#\s*DISABLE',
            r'>>>\s*#\s*UNSTABLE',
            r'>>>\s*#\s*FAILING',
            r'>>>\s*#\s*SCRIPT',
            r'>>>\s*#\s*SLOW_DOCTEST',
            # r'>>>\s*#\s*x?doctest:\s\+SKIP',
        ]
        if pytest:
            disable_patterns += [
                r'>>>\s*#\s*pytest.skip'
            ]

        pattern = '|'.join(disable_patterns)
        m = re.match(pattern, self.docsrc, flags=re.IGNORECASE)
        return m is not None

    @property
    def unique_callname(self):
        return self.callname + ':' + str(self.num)

    @property
    def node(self):
        """ this pytest node """
        return self.modpath + '::' + self.callname + ':' + str(self.num)

    @property
    def valid_testnames(self):
        return {
            self.callname,
            self.unique_callname,
        }

    def wants(self):
        """
        Returns a list of the populated wants
        """
        self._parse()
        for part in self._parts:
            if part.want:
                yield part.want

    def format_parts(self, linenos=True, colored=None, want=True,
                     offset_linenos=None, prefix=True):
        """ used by format_src """
        self._parse()
        colored = self.config.getvalue('colored', colored)
        partnos = self.config.getvalue('partnos')
        offset_linenos = self.config.getvalue('offset_linenos', offset_linenos)

        n_digits = None
        startline = 1
        if linenos:
            if offset_linenos:
                startline = self.lineno
            n_lines = sum(p.n_lines for p in self._parts)
            endline = startline + n_lines

            n_digits = math.log(max(1, endline), 10)
            n_digits = int(math.ceil(n_digits))

        for part in self._parts:
            part_text = part.format_part(linenos=linenos, want=want,
                                         startline=startline,
                                         n_digits=n_digits, prefix=prefix,
                                         colored=colored, partnos=partnos)
            yield part_text

    def format_src(self, linenos=True, colored=None, want=True,
                   offset_linenos=None, prefix=True):
        """
        Adds prefix and line numbers to a doctest

        Args:
            linenos (bool): if True, adds line numbers to output

            colored (bool): if True highlight text with ansi colors. Default
                is specified in the config.

            want (bool): if True includes "want" lines (default False).

            offset_linenos (bool): if True offset line numbers to agree with
                their position in the source text file (default False).

            prefix (bool): if False, exclude the doctest `>>> ` prefix

        Example:
            >>> from xdoctest.core import *
            >>> from xdoctest import core
            >>> testables = parse_doctestables(core.__file__)
            >>> self = next(testables)
            >>> self._parse()
            >>> print(self.format_src())
            >>> print(self.format_src(linenos=False, colored=False))
            >>> assert not self.is_disabled()
        """
        formated_parts = list(self.format_parts(linenos=linenos,
                                                colored=colored, want=want,
                                                offset_linenos=offset_linenos,
                                                prefix=prefix))
        full_source = '\n'.join(formated_parts)
        return full_source

    def _parse(self):
        """
        Divide the given string into examples and intervening text.

        Example:
            >>> s = 'I am a dummy example with three parts'
            >>> x = 10
            >>> print(s)
            I am a dummy example with three parts
            >>> s = 'My purpose it so demonstrate how wants work here'
            >>> print('The new want applies ONLY to stdout')
            >>> print('given before the last want')
            >>> '''
                this wont hurt the test at all
                even though its multiline '''
            >>> y = 20
            The new want applies ONLY to stdout
            given before the last want
            >>> # Parts from previous examples are executed in the same context
            >>> print(x + y)
            30

            this is simply text, and doesnt apply to the previous doctest the
            <BLANKLINE> directive is still in effect.

        Example:
            >>> from xdoctest import parser
            >>> from xdoctest.docstr import docscrape_google
            >>> from xdoctest import doctest_example
            >>> DocTest = doctest_example.DocTest
            >>> docstr = DocTest._parse.__doc__
            >>> blocks = docscrape_google.split_google_docblocks(docstr)
            >>> doclineno = DocTest._parse.__code__.co_firstlineno
            >>> key, (docsrc, offset) = blocks[-2]
            >>> lineno = doclineno + offset
            >>> self = DocTest(docsrc, doctest_example.__file__, '_parse', 0,
            >>>                lineno)
            >>> self._parse()
            >>> assert len(self._parts) >= 3
            >>> #p1, p2, p3 = self._parts
            >>> self.run()
        """
        if not self._parts:
            info = dict(callname=self.callname, modpath=self.modpath,
                        lineno=self.lineno, fpath=self.fpath)
            self._parts = parser.DoctestParser().parse(self.docsrc, info)
            self._parts = [p for p in self._parts
                           if not isinstance(p, six.string_types)]
        # Ensure part numbers are given
        for partno, part in enumerate(self._parts):
            part.partno = partno

    def _import_module(self):
        if self.module is None:
            if not self.modname.startswith('<'):
                self.module = utils.import_module_from_path(self.modpath)

    def _extract_future_flags(self, globs):
        """
        Return the compiler-flags associated with the future features that
        have been imported into the given namespace (globs).
        """
        compileflags = 0
        for key in __future__.all_feature_names:
            feature = globs.get(key, None)
            if feature is getattr(__future__, key):
                compileflags |= feature.compiler_flag
        return compileflags

    def _test_globals(self):
        test_globals = self.globs
        if self.module is None:
            compileflags = 0
        else:
            test_globals.update(self.module.__dict__)
            compileflags = self._extract_future_flags(test_globals)
        # force print function and division futures
        compileflags |= __future__.print_function.compiler_flag
        compileflags |= __future__.division.compiler_flag
        return test_globals, compileflags

    def anything_ran(self):
        # If everything was skipped, then there will be no stdout
        return len(self.logged_stdout) > 0

    def run(self, verbose=None, on_error=None):
        """
        Executes the doctest, checks the results, reports the outcome.
        """
        on_error = self.config.getvalue('on_error', on_error)
        verbose = self.config.getvalue('verbose', verbose)
        if on_error not in {'raise', 'return'}:
            raise KeyError(on_error)

        self._parse()  # parse out parts if we have not already done so
        self.pre_run(verbose)
        self._import_module()

        # Prepare for actual test run
        test_globals, compileflags = self._test_globals()

        self.logged_evals.clear()
        self.logged_stdout.clear()
        self._unmatched_stdout = []

        self.skipped_parts = []
        self.exc_info = None
        self._suppressed_stdout = verbose <= 1

        # Initialize a new runtime state
        default_state = self.config['default_runtime_state']
        runstate = self._runstate = directive.RuntimeState(default_state)
        # setup reporting choice
        runstate.set_report_style(self.config['reportchoice'])

        # Can't do this because we can't force execution of SCRIPTS
        # if self.is_disabled():
        #     runstate['SKIP'] = True

        # Use the same capture object for all parts in the test
        cap = utils.CaptureStdout(supress=self._suppressed_stdout)
        with warnings.catch_warnings(record=True) as self.warn_list:
            for partx, part in enumerate(self._parts):
                # Extract directives and and update runtime state
                runstate.update(part.directives)

                # Handle runtime actions
                if runstate['SKIP']:
                    self.skipped_parts.append(part)
                    continue

                # Prepare to capture stdout and evaluated values
                self.failed_part = part
                got_eval = constants.NOT_EVALED
                try:
                    # Compile code, handle syntax errors
                    mode = 'eval' if part.use_eval else 'exec'
                    self._partfilename = '<doctest:' + self.node + '>'
                    # Hack: we will prefix souce with newlines to
                    # make the exec line number match.
                    code = compile(
                        # '\n' * part.line_offset +
                        part.source, mode=mode,
                        filename=self._partfilename,
                        flags=compileflags, dont_inherit=True
                    )
                except KeyboardInterrupt:  # nocover
                    raise
                except Exception:
                    self.exc_info = sys.exc_info()
                    ex_type, ex_value, tb = self.exc_info
                    self.tb_lineno = tb.tb_lineno
                    if on_error == 'raise':
                        raise
                try:
                    # Execute the doctest code
                    try:
                        # NOTE: For code passed to eval or exec, there is no
                        # difference between locals and globals. Only pass in
                        # one dict, otherwise there is weird behavior
                        with cap:
                            # We can execute each part using exec or eval.  If
                            # a doctest part is flagged as `use_eval` we
                            # exepect it to return an object with a repr that
                            # can compared to a "want" statement.
                            if part.use_eval:
                                got_eval = eval(code, test_globals)
                            else:
                                exec(code, test_globals)

                        # Record any standard output and "got_eval" produced by
                        # this doctest_part.
                        self.logged_evals[partx] = got_eval
                        self.logged_stdout[partx] = cap.text
                    except Exception as ex:
                        if part.want:
                            # A failure may be expected if the traceback
                            # matches the part's want statement.
                            exception = sys.exc_info()
                            exc_got = traceback.format_exception_only(*exception[:2])[-1]
                            checker.check_exception(exc_got, part.want, runstate)
                        else:
                            raise
                    else:
                        """
                        TODO:
                            [ ] - Delay got-want failure until the end of the
                            doctest. Allow the rest of the code to run.  If
                            multiple errors occur, show them both.
                        """
                        if part.want:
                            got_stdout = cap.text
                            if not runstate['IGNORE_WANT']:
                                part.check(got_stdout, got_eval, runstate,
                                           unmatched=self._unmatched_stdout)
                            # Clear unmatched output when a check passes
                            self._unmatched_stdout = []
                        else:
                            # If a part doesnt have a want allow its output to
                            # be matched by the next part.
                            self._unmatched_stdout.append(cap.text)

                # Handle anything that could go wrong
                except KeyboardInterrupt:  # nocover
                    raise
                except (exceptions.ExitTestException,
                        exceptions._pytest.outcomes.Skipped):
                    if verbose > 0:
                        print('Test gracefully exists')
                    break
                except parser.GotWantException:
                    self.exc_info = sys.exc_info()
                    if on_error == 'raise':
                        raise
                    break
                except Exception:
                    ex_type, ex_value, tb = sys.exc_info()
                    # The idea of CLEAN_TRACEBACK is to make it so the
                    # traceback from this function doesn't clutter the error
                    # message the user sees. However, I haven't been able to
                    # make it work yet.
                    CLEAN_TRACEBACK = 0
                    if CLEAN_TRACEBACK:
                        # Pop the eval off the stack
                        if tb.tb_next is not None:
                            tb = tb.tb_next
                        self.tb_lineno = tb.tb_lineno
                        # tb.tb_lineno = (tb_lineno +
                        #                 self.failed_part.line_offset + self.lineno)
                    else:
                        if tb.tb_next is not None:
                            # Use the next to pop the eval of the stack
                            self.tb_lineno = tb.tb_next.tb_lineno
                        else:
                            # TODO: test and understand this case
                            self.tb_lineno = tb.tb_lineno

                    self.exc_info = (ex_type, ex_value, tb)
                    if on_error == 'raise':
                        raise
                    break
                finally:
                    assert cap.text is not None
                    # Ensure that we logged the output even in failure cases
                    self.logged_evals[partx] = got_eval
                    self.logged_stdout[partx] = cap.text

        if self.exc_info is None:
            self.failed_part = None

        if len(self.skipped_parts) == len(self._parts):
            # we skipped everything
            if self.mode == 'pytest':
                import pytest
                pytest.skip()

        return self.post_run(verbose)

    @property
    def cmdline(self):
        if self.mode == 'pytest':
            return 'pytest ' + self.node
        elif self.mode == 'native':
            in_path = static.is_modname_importable(self.modname)
            if in_path:
                # should be able to find the module by name
                return 'python -m xdoctest ' + self.modname + ' ' + self.unique_callname
            else:
                # needs the full path to be able to run the module
                return 'python -m xdoctest ' + self.modpath + ' ' + self.unique_callname
        else:
            raise KeyError(self.mode)

    @property
    def block_prefix(self):
        return 'ZERO-ARG' if self.block_type == 'zero-arg' else 'DOCTEST'

    def pre_run(self, verbose):
        if verbose >= 1:
            if verbose >= 2:
                barrier = self._color('====== <exec> ======', 'white')
                print(barrier)
            if self.block_type == 'zero-arg':
                # zero-arg funcs arent doctests, but we can still run them
                print('* ZERO-ARG FUNC : {}'.format(self.node))
            else:
                print('* DOCTEST : {}, line {}'.format(self.node, self.lineno) + self._color(' <- wrt source file', 'white'))
            if verbose >= 3:
                print(self._color(self.block_prefix + ' SOURCE', 'white'))
                print(self.format_src())
                # print(self._color('* ----------', 'white'))
                print(self._color(self.block_prefix + ' STDOUT/STDERR', 'white'))

    def failed_line_offset(self):
        """
        Determine which line in the doctest failed.
        """
        if self.exc_info is None:
            return None
        else:
            ex_type, ex_value, tb = self.exc_info
            offset = self.failed_part.line_offset
            if isinstance(ex_value, parser.GotWantException):
                # Return the line of the want line
                offset += self.failed_part.n_exec_lines + 1
            else:
                offset += self.tb_lineno
            offset -= 1
            return offset

    def failed_lineno(self):
        offset = self.failed_line_offset()
        if offset is None:
            return None
        else:
            # Find the first line of the part
            lineno = self.lineno + offset
            return lineno

    def repr_failure(self, with_tb=True):
        r"""
        Constructs lines detailing information about a failed doctest

        CommandLine:
            python -m xdoctest.core DocTest.repr_failure:0
            python -m xdoctest.core DocTest.repr_failure:1
            python -m xdoctest.core DocTest.repr_failure:2

        Example:
            >>> from xdoctest.core import *
            >>> docstr = utils.codeblock(
                '''
                >>> x = 1
                >>> print(x + 1)
                2
                >>> print(x + 3)
                3
                >>> print(x + 100)
                101
                ''')
            >>> parsekw = dict(fpath='foo.txt', callname='bar', lineno=42)
            >>> self = list(parse_docstr_examples(docstr, **parsekw))[0]
            >>> summary = self.run(on_error='return', verbose=0)
            >>> print('[res]' + '\n[res]'.join(self.repr_failure()))

        Example:
            >>> from xdoctest.core import *
            >>> docstr = utils.codeblock(
                r'''
                >>> 1
                1
                >>> print('.▴  .\n.▴ ▴.') # xdoc: -NORMALIZE_WHITESPACE
                . ▴ .
                .▴ ▴.
                ''')
            >>> parsekw = dict(fpath='foo.txt', callname='bar', lineno=42)
            >>> self = list(parse_docstr_examples(docstr, **parsekw))[0]
            >>> summary = self.run(on_error='return', verbose=1)
            >>> print('[res]' + '\n[res]'.join(self.repr_failure()))


        Example:
            >>> from xdoctest.core import *
            >>> docstr = utils.codeblock(
                '''
                >>> assert True
                >>> assert False
                >>> x = 100
                ''')
            >>> self = list(parse_docstr_examples(docstr))[0]
            >>> summary = self.run(on_error='return', verbose=0)
            >>> print('[res]' + '\n[res]'.join(self.repr_failure()))
        """
        #     '=== LINES ===',
        # ]

        # if '--xdoc-debug' in sys.argv:
        #     lines += ['DEBUG PARTS: ']
        #     for partx, part in enumerate(self._parts):
        #         lines += [str(partx) + ': ' + str(part)]
        #         lines += ['  use_eval: {!r}'.format(part.use_eval)]
        #         lines += ['  directives: {!r}'.format(part.directives)]
        #         lines += ['  want: {!r}'.format(str(part.want)[0:25])]
        #         val = self.logged_evals.get(partx, None)
        #         lines += ['  eval: ' + repr(val)]
        #         val = self.logged_stdout.get(partx, None)
        #         lines += ['  stdout: ' + repr(val)]
        #     partx = self._parts.index(self.failed_part)
        #     lines += [
        #         'failed partx = {}'.format(partx)
        #     ]
        #     failed_part = self.failed_part
        #     lines += ['----']
        #     lines += ['Failed part line offset:']
        #     lines += ['{}'.format(failed_part.line_offset)]
        #     lines += ['Failed directives:']
        #     lines += ['{}'.format(list(failed_part.directives))]

        #     lines += ['Failed part source:']
        #     lines += failed_part.source.splitlines()
        #     lines += ['Failed part want:']
        #     if failed_part.want_lines:
        #         lines += failed_part.want_lines
        #     lines += ['Failed part stdout:']
        #     lines += self.logged_stdout[partx].splitlines()
        #     lines += ['Failed part eval:']
        #     lines += [repr(self.logged_evals[partx])]
        #     lines += ['----']

        #     lines += [
        #         # 'self.module = {}'.format(self.module),
        #         # 'self.modpath = {}'.format(self.modpath),
        #         # 'self.modpath = {}'.format(self.modname),
        #         # 'self.globs = {}'.format(self.globs.keys()),
        #     ]
        # lines += ['Failed doctest in ' + self.callname]

        if self.exc_info is None:
            return []
        ex_type, ex_value, tb = self.exc_info
        # Failure line offset wrt the doctest (starts from 0)
        fail_offset = self.failed_line_offset()
        # Failure line number wrt the entire file (starts from 1)
        fail_lineno = self.failed_lineno()

        lines = [
            '* REASON: {}'.format(ex_type.__name__),
            self._color(self.block_prefix + ' DEBUG INFO', 'white'),
            '  XDoc "{}", line {}'.format(self.node, fail_offset + 1) +
            self._color(' <- wrt doctest', 'red'),
        ]

        colored = self.config['colored']
        if fail_lineno is not None:
            fpath = '<file?>' if self.fpath is None else self.fpath
            lines += ['  File "{}", line {},'.format(fpath, fail_lineno) +
                      self._color(' <- wrt source file', 'red')]

        # lines += ['  in doctest "{}", line {}'.format(self.unique_callname,
        #                                               fail_offset + 1) +
        #           self._color(' <- relative line number in the docstest', 'red')]

        # source_text = self.format_src(colored=colored, linenos=True,
        #                               want=False)
        # source_text = utils.indent(source_text)
        # lines += source_text.splitlines()

        def r1_strip_nl(text):
            return text[:-1] if text.endswith('\n') else text

        # if self.logged_stdout:
        #     lines += ['stdout results:']
        #     lines += [r1_strip_nl(t) for t in self.logged_stdout.values() if t]

        textgen = self.format_parts(colored=colored, linenos=True,
                                    want=False)

        n_digits = 1

        # Logic to break output between pass, failed, and unexecuted parts
        before_part_lines = []
        fail_part_lines = []
        after_parts_lines = []
        temp = [before_part_lines, fail_part_lines, after_parts_lines]
        tindex = 0
        indent_text = ' ' * (5 + n_digits)

        for partx, (part, part_text) in enumerate(zip(self._parts, textgen)):
            if part in self.skipped_parts:
                # temp[tindex] += [utils.indent(part_text, ' ' * 4)]
                # temp[tindex] += [utils.indent(' >>> # skipped', indent_text)]
                continue
            part_out = r1_strip_nl(self.logged_stdout.get(partx, ''))
            if part is self.failed_part:
                tindex += 1
            # Append the part source code
            temp[tindex] += [utils.indent(part_text, ' ' * 4)]
            # Append the part stdout (if it exists)
            if part_out:
                temp[tindex] += [utils.indent(part_out, indent_text)]
            if part is self.failed_part:
                tindex += 1
            # part_eval = self.logged_evals[partx]
            # if part_eval is not NOT_EVALED:
            #     temp[tindex] += [repr(part_eval)]

        lines += [self._color(self.block_prefix + ' PART BREAKDOWN', 'white')]
        if before_part_lines:
            lines += ['Passed Parts:']
            lines += before_part_lines

        if fail_part_lines:
            lines += ['Failed Part:']
            lines += fail_part_lines

        if after_parts_lines:
            lines += ['Remaining Parts:']
            lines += after_parts_lines

        lines += [self._color(self.block_prefix + ' TRACEBACK', 'white')]
        if hasattr(ex_value, 'output_difference'):
            lines += [
                ex_value.output_difference(self._runstate, colored=colored),
                ex_value.output_repr_difference(self._runstate)
            ]
        else:
            if with_tb:
                # TODO: enhance formatting to show an IPython-like output of
                # where the error occurred in the doctest
                tblines = traceback.format_exception(*self.exc_info)

                def _alter_traceback_linenos(self, tblines):

                    def overwrite_lineno(linepart):
                        # Replace the trailing part which is the lineno
                        old_linestr = linepart[-1]  # noqa

                        # This is the lineno we will insert
                        rel_lineno = self.failed_part.line_offset + tb_lineno
                        abs_lineno = self.lineno + rel_lineno - 1

                        new_linestr = 'rel: {rel}, abs: {abs}'.format(
                            rel=rel_lineno,
                            abs=abs_lineno,
                        )

                        linepart = linepart[:-1] + [new_linestr]
                        return linepart

                    new_tblines = []
                    for i, line in enumerate(tblines):

                        if 'xdoctest/xdoctest/doctest_example' in line:
                            # hack, remove ourselves from the tracback
                            continue
                            # new_tblines.append('!!!!!')
                            # raise Exception('foo')
                            # continue

                        if self._partfilename in line:
                            # Intercept the line corresponding to the doctest
                            tbparts = line.split(',')
                            tb_lineno = int(tbparts[-2].strip().split()[1])
                            # modify the line number to match the doctest
                            linepart = tbparts[-2].split(' ')

                            linepart = overwrite_lineno(linepart)

                            tbparts[-2] = ' '.join(linepart)
                            new_line = ','.join(tbparts)

                            # failed_ctx = '>>> ' + self.failed_part.exec_lines[tb_lineno - 1]
                            failed_ctx = self.failed_part.orig_lines[tb_lineno - 1]
                            extra = '    ' + failed_ctx
                            line = (new_line + extra + '\n')

                        # m = '(t{})'.format(i)
                        # line = m + line.replace('\n', '\n' + m)
                        new_tblines.append(line)

                    return new_tblines

                new_tblines = _alter_traceback_linenos(self, tblines)

                if colored:
                    tbtext = '\n'.join(new_tblines)
                    tbtext = utils.highlight_code(tbtext, lexer_name='pytb',
                                                  stripall=True)
                    new_tblines = tbtext.splitlines()
                lines += new_tblines

        lines += [self._color(self.block_prefix + ' REPRODUCTION', 'white')]
        lines += ['CommandLine:']
        lines += ['    ' + self.cmdline]
        return lines

    def _print_captured(self):
        out_text = ''.join([v for v in self.logged_stdout.values() if v])
        if out_text is not None:
            assert isinstance(out_text, six.text_type), 'do not use ascii'
        try:
            print(out_text)
        except UnicodeEncodeError:
            print('Weird travis bug')
            print('type(out_text) = %r' % (type(out_text),))
            print('out_text = %r' % (out_text,))

    def _color(self, text, color, enabled=None):
        """ conditionally color text based on config and flags """
        colored = self.config.getvalue('colored', enabled)
        if colored:
            text = utils.color_text(text, color)
        return text

    def post_run(self, verbose):
        # print('POST RUN verbose = {!r}'.format(verbose))
        summary = {
            'passed': self.exc_info is None
        }
        if verbose >= 1:
            print(self._color(self.block_prefix + ' RESULT', 'white'))
        if self.exc_info is None:
            if verbose >= 1:
                if self._suppressed_stdout:
                    self._print_captured()
                if len(self.skipped_parts) == len(self._parts):
                    success = self._color('SKIPPED', 'yellow')
                else:
                    success = self._color('SUCCESS', 'green')
                print('* {}: {}'.format(success, self.node))
        else:
            if verbose >= 1:
                failure = self._color('FAILURE', 'red')
                print('* {}: {}'.format(failure, self.node))

                if verbose >= 2:
                    lines = self.repr_failure()
                    text = '\n'.join(lines)
                    print(text)
            summary['exc_info'] = self.exc_info
        if verbose >= 2:
            barrier = self._color('====== </exec> ======', 'white')
            print(barrier)
        return summary


if __name__ == '__main__':
    r"""
    CommandLine:
        python -m xdoctest.doctest_example
    """
    import xdoctest
    xdoctest.doctest_module(__file__)
