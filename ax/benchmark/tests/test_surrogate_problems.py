# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.


import numpy as np
from ax.benchmark.benchmark import compute_score_trace
from ax.core.runner import Runner
from ax.utils.common.testutils import TestCase
from ax.utils.testing.benchmark_stubs import get_moo_surrogate, get_soo_surrogate


class TestSurrogateProblems(TestCase):
    def test_lazy_instantiation(self) -> None:

        # test instantiation from init
        sbp = get_soo_surrogate()

        self.assertIsNone(sbp._runner)
        # sets runner
        self.assertIsInstance(sbp.runner, Runner)

        self.assertIsNotNone(sbp._runner)
        self.assertIsNotNone(sbp.runner)

        # repeat for MOO
        sbp = get_moo_surrogate()

        self.assertIsNone(sbp._runner)
        # sets runner
        self.assertIsInstance(sbp.runner, Runner)

        self.assertIsNotNone(sbp._runner)
        self.assertIsNotNone(sbp.runner)

    def test_compute_score_trace(self) -> None:
        soo_problem = get_soo_surrogate()
        score_trace = compute_score_trace(
            np.arange(10),
            num_baseline_trials=5,
            problem=soo_problem,
        )
        self.assertTrue(np.isfinite(score_trace).all())

        moo_problem = get_moo_surrogate()

        score_trace = compute_score_trace(
            np.arange(10), num_baseline_trials=5, problem=moo_problem
        )
        self.assertTrue(np.isfinite(score_trace).all())
