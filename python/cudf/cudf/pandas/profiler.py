# SPDX-FileCopyrightText: Copyright (c) 2023 NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

import inspect
import pickle
import sys
import time
from collections import defaultdict
from typing import Union

from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from .fast_slow_proxy import (
    _FinalProxy,
    _FunctionProxy,
    _IntermediateProxy,
    _MethodProxy,
)

# This text is used in contexts where the profiler is injected into the
# original code. The profiler is injected at the top of the cell, so the line
# numbers in the profiler results are offset by 2.
_profile_injection_text = """\
from cudf.pandas import Profiler
with Profiler() as profiler:
{original_lines}

# Patch the results to shift the line numbers back to the original before the
# profiler injection.
new_results = {{}}

for (lineno, currfile, line), v in profiler._results.items():
    new_results[(lineno - 2, currfile, line)] = v

profiler._results = new_results
profiler.print_per_line_stats()
{function_profile_printer}
"""


def lines_with_profiling(lines, print_function_profile=False):
    """Inject profiling code into the given lines of code."""
    cleaned_lines = "\n".join(
        [(" " * 4) + line.replace("\t", " " * 4) for line in lines]
    )
    return _profile_injection_text.format(
        original_lines=cleaned_lines,
        function_profile_printer="profiler.print_per_function_stats()"
        if print_function_profile
        else "",
    )


class Profiler:
    _IGNORE_LIST = ["Profiler()", "settrace(None)"]

    def __init__(self):
        self._results = {}
        # Map func-name to list of calls (was_fast, time)
        self._per_func_results = defaultdict(list)
        # Current fast_slow_function_call stack frame recording name
        # and start time
        self._call_stack = []
        self._currkey = None
        self._timer = {}
        self._currfile = None
        self.start_time = None
        self.end_time = None

    def __enter__(self, *args, **kwargs):
        self.start_time = time.perf_counter()
        self._oldtrace = sys.gettrace()
        # Setting the global trace function with sys.settrace does not affect
        # the current call stack, so in addition to this we must also set the
        # current frame's f_trace attribute as done below.
        sys.settrace(self._tracefunc)

        # Following excerpt from:
        # https://docs.python.org/3/library/sys.html#sys.settrace
        # For more fine-grained usage, it is possible
        # to set a trace function by assigning
        # frame.f_trace = tracefunc explicitly, rather than
        # relying on it being set indirectly via the return
        # value from an already installed trace function
        # Hence we need to perform `f_trace = self._tracefunc`
        # we need to `f_back` because current frame will be
        # of this file.
        frame = inspect.currentframe().f_back
        self._currfile = frame.f_code.co_filename
        self._f_back_oldtrace = frame.f_trace
        frame.f_trace = self._tracefunc
        return self

    def __exit__(self, *args, **kwargs):
        sys.settrace(self._oldtrace)
        inspect.currentframe().f_back.f_trace = self._f_back_oldtrace
        self.end_time = time.perf_counter()

    @staticmethod
    def get_namespaced_function_name(
        func_obj: Union[
            _FunctionProxy,
            _MethodProxy,
            type[_FinalProxy],
            type[_IntermediateProxy],
        ]
    ):
        if isinstance(func_obj, _MethodProxy):
            # Extract classname from method object
            type_name = type(func_obj._xdf_wrapped.__self__).__name__
            # Explicitly ask for __name__ on _xdf_wrapped to avoid
            # getting a private attribute and forcing a slow-path copy
            func_name = func_obj._xdf_wrapped.__name__
            return ".".join([type_name, func_name])
        elif isinstance(func_obj, _FunctionProxy) or issubclass(
            func_obj, (_FinalProxy, _IntermediateProxy)
        ):
            return func_obj.__name__
        else:
            raise NotImplementedError(
                f"Don't know how to get namespaced name for {func_obj}"
            )

    def _tracefunc(self, frame, event, arg):
        if event == "line" and frame.f_code.co_filename == self._currfile:
            key = "".join(inspect.stack()[1].code_context)
            if not any(
                ignore_word in key for ignore_word in Profiler._IGNORE_LIST
            ):
                self._currkey = (frame.f_lineno, self._currfile, key)
                self._results.setdefault(self._currkey, {})
                self._timer[self._currkey] = time.perf_counter()
        elif (
            event == "call"
            and frame.f_code.co_name == "_fast_slow_function_call"
        ):
            if self._currkey is not None:
                self._timer[self._currkey] = time.perf_counter()

            # Store per-function information for free functions and methods
            frame_locals = inspect.getargvalues(frame).locals
            if (
                isinstance(
                    func_obj := frame_locals["args"][0],
                    (_MethodProxy, _FunctionProxy),
                )
                or isinstance(func_obj, type)
                and issubclass(func_obj, (_FinalProxy, _IntermediateProxy))
            ):
                func_name = self.get_namespaced_function_name(func_obj)
                self._call_stack.append((func_name, time.perf_counter()))
        elif (
            event == "return"
            and frame.f_code.co_name == "_fast_slow_function_call"
        ):
            if arg is None:
                # Call raised an exception, just pop the stack
                self._call_stack.pop()
            else:
                if self._currkey is not None:
                    if arg[1]:  # fast
                        run_time = (
                            time.perf_counter() - self._timer[self._currkey]
                        )
                        self._results[self._currkey][
                            "gpu_time"
                        ] = run_time + self._results[self._currkey].get(
                            "gpu_time", 0
                        )
                    else:
                        run_time = (
                            time.perf_counter() - self._timer[self._currkey]
                        )
                        self._results[self._currkey][
                            "cpu_time"
                        ] = run_time + self._results[self._currkey].get(
                            "cpu_time", 0
                        )

                frame_locals = inspect.getargvalues(frame).locals
                if (
                    isinstance(
                        func_obj := frame_locals["args"][0],
                        (_MethodProxy, _FunctionProxy),
                    )
                    or isinstance(func_obj, type)
                    and issubclass(func_obj, (_FinalProxy, _IntermediateProxy))
                ):
                    func_name, start = self._call_stack.pop()
                    self._per_func_results[func_name].append(
                        (arg[1], time.perf_counter() - start)
                    )

        return self._tracefunc

    @property
    def per_line_stats(self):
        list_data = []
        for key, val in self._results.items():
            cpu_time = val.get("cpu_time", 0)
            gpu_time = val.get("gpu_time", 0)
            line_no, _, line = key
            list_data.append([line_no, line, gpu_time, cpu_time])

        return sorted(list_data, key=lambda line: line[0])

    @property
    def per_function_stats(self):
        data = {}
        for func_name, func_data in self._per_func_results.items():
            data[func_name] = {"cpu": [], "gpu": []}

            for is_gpu, runtime in func_data:
                key = "gpu" if is_gpu else "cpu"
                data[func_name][key].append(runtime)
        return data

    def print_per_line_stats(self):
        table = Table()
        table.add_column("Line no.")
        table.add_column("Line")
        table.add_column("GPU TIME(s)")
        table.add_column("CPU TIME(s)")
        for line_no, line, gpu_time, cpu_time in self.per_line_stats:
            table.add_row(
                str(line_no),
                Syntax(str(line), "python"),
                "" if gpu_time == 0 else "{:.9f}".format(gpu_time),
                "" if cpu_time == 0 else "{:.9f}".format(cpu_time),
            )
        time_elapsed = self.end_time - self.start_time
        table.title = f"""\n\
        Total time elapsed: {time_elapsed:.3f} seconds

        Stats
        """
        console = Console()
        console.print(table)

    def print_per_function_stats(self):
        n_gpu_func_calls = 0
        n_cpu_func_calls = 0
        total_gpu_time = 0
        total_cpu_time = 0

        table = Table()
        for col in (
            "Function",
            "GPU ncalls",
            "GPU cumtime",
            "GPU percall",
            "CPU ncalls",
            "CPU cumtime",
            "CPU percall",
        ):
            table.add_column(col)

        for func_name, func_data in self.per_function_stats.items():
            gpu_times = func_data["gpu"]
            cpu_times = func_data["cpu"]
            table.add_row(
                func_name,
                f"{len(gpu_times)}",
                f"{sum(gpu_times):.3f}",
                f"{sum(gpu_times) / max(len(gpu_times), 1):.3f}",
                f"{len(cpu_times)}",
                f"{sum(cpu_times):.3f}",
                f"{sum(cpu_times) / max(len(cpu_times), 1):.3f}",
            )
            total_gpu_time += sum(gpu_times)
            total_cpu_time += sum(cpu_times)
            n_gpu_func_calls += len(gpu_times)
            n_cpu_func_calls += len(cpu_times)

        time_elapsed = self.end_time - self.start_time
        table.title = f"""\n\
        Total time elapsed: {time_elapsed:.3f} seconds
        {n_gpu_func_calls} GPU function calls in {total_gpu_time:.3f} seconds
        {n_cpu_func_calls} CPU function calls in {total_cpu_time:.3f} seconds

        Stats
        """
        console = Console()
        console.print(table)

    def dump_stats(self, file_name):
        with open(file_name, "wb") as f:
            pickle.dump(self, f)


def load_stats(file_name):
    pickle_file = open(file_name, "rb")
    return pickle.load(pickle_file)
