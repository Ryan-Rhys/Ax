# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from copy import deepcopy
from unittest.mock import Mock

import numpy as np
from ax.core import BatchTrial
from ax.core.observation import (
    Observation,
    ObservationData,
    ObservationFeatures,
    observations_from_data,
    recombine_observations,
)
from ax.core.outcome_constraint import OutcomeConstraint
from ax.core.types import ComparisonOp
from ax.metrics.branin import BraninMetric
from ax.modelbridge import ModelBridge
from ax.modelbridge.registry import Models
from ax.modelbridge.transforms.relativize import Relativize
from ax.models.base import Model
from ax.utils.common.testutils import TestCase
from ax.utils.common.typeutils import checked_cast, not_none
from ax.utils.stats.statstools import relativize_data
from ax.utils.testing.core_stubs import (
    get_branin_data_batch,
    get_branin_experiment,
    get_branin_multi_objective_optimization_config,
    get_branin_optimization_config,
    get_branin_with_multi_task,
    get_search_space,
)
from hypothesis import assume, given, settings, strategies as st
from numpy import int64


class RelativizeDataTest(TestCase):
    def test_relativize_transform_requires_a_modelbridge(self) -> None:
        with self.assertRaisesRegex(ValueError, "modelbridge"):
            Relativize(
                search_space=None,
                observations=[],
            )

    def test_relativize_transform_requires_a_modelbridge_to_have_status_quo_data(
        self,
    ) -> None:
        # modelbridge has no status quo
        sobol = Models.SOBOL(search_space=get_search_space())
        self.assertIsNone(sobol.status_quo)
        with self.assertRaisesRegex(ValueError, "status quo data"):
            Relativize(
                search_space=None,
                observations=[],
                modelbridge=sobol,
            ).transform_observations(
                observations=[
                    Observation(
                        data=ObservationData(
                            metric_names=["foo"],
                            means=np.array([2]),
                            covariance=np.array([[0.1]]),
                        ),
                        features=ObservationFeatures(parameters={"x": 1}),
                        arm_name="0_0",
                    )
                ],
            )

        # modelbridge has status quo
        exp = get_branin_experiment(
            with_batch=True,
            with_status_quo=True,
        )
        # making status_quo out of design
        not_none(exp._status_quo)._parameters["x1"] = 10000.0
        for t in exp.trials.values():
            t.mark_running(no_runner_required=True)
            exp.attach_data(get_branin_data_batch(batch=checked_cast(BatchTrial, t)))
            t.mark_completed()
        data = exp.fetch_data()
        modelbridge = ModelBridge(
            search_space=exp.search_space,
            model=Model(),
            transforms=[Relativize],
            experiment=exp,
            data=data,
        )
        mean_in_data = data.df.query(f"arm_name == '{not_none(exp.status_quo).name}'")[
            "mean"
        ].item()
        # modelbridge.status_quo_data_by_trial is accurate
        self.assertEqual(
            mean_in_data, not_none(modelbridge.status_quo_data_by_trial)[0].means[0]
        )

        # create a new experiment
        new_exp = get_branin_experiment(
            with_batch=True,
            with_status_quo=True,
        )
        for t in new_exp.trials.values():
            t.mark_running(no_runner_required=True)
            new_exp.attach_data(
                get_branin_data_batch(batch=checked_cast(BatchTrial, t))
            )
            t.mark_completed()
        new_data = new_exp.fetch_data()
        new_observations = observations_from_data(experiment=new_exp, data=new_data)
        # calls modelbridge._set_training_data inside
        modelbridge._set_training_data(
            observations=new_observations, search_space=new_exp.search_space
        )
        # The new data is different from the original data
        self.assertNotEqual(data, new_data)
        self.assertFalse(data.df.equals(new_data.df))
        mean_in_data = new_data.df.query(
            f"arm_name == '{not_none(new_exp.status_quo).name}'"
        )["mean"].item()
        # modelbridge.status_quo_data_by_trial remains accurate
        self.assertEqual(
            mean_in_data, not_none(modelbridge.status_quo_data_by_trial)[0].means[0]
        )

        # Can still find status_quo_data_by_trial when status_quo name is None
        mb_sq = not_none(modelbridge._status_quo)
        mb_sq.arm_name = None
        self.assertIsNotNone(modelbridge.status_quo_data_by_trial)

        # test transform edge cases
        observations = observations_from_data(
            experiment=exp,
            data=data,
        )
        tf = Relativize(
            search_space=None,
            observations=observations,
            modelbridge=modelbridge,
        )
        # making observation coming from trial_index not in modelbridge
        observations[0].features.trial_index = int64(999)
        self.assertRaises(ValueError, tf.transform_observations, observations)

    def test_relativize_transform_observations(self) -> None:
        metric_names = ["foobar", "foobaz"]
        arm_names = ["status_quo", "0_0"]
        obs_data = [
            ObservationData(
                metric_names=metric_names,
                means=np.array([2, 5]),
                covariance=np.array([[0.1, 0.0], [0.0, 0.2]]),
            ),
            ObservationData(
                metric_names=metric_names,
                means=np.array([1.0, 10.0]),
                covariance=np.array([[0.3, 0.0], [0.0, 0.4]]),
            ),
        ]
        obs_features = [
            # pyre-fixme[6]: For 2nd param expected `Optional[int64]` but got `int`.
            ObservationFeatures(parameters={"x": 1}, trial_index=0),
            # pyre-fixme[6]: For 2nd param expected `Optional[int64]` but got `int`.
            ObservationFeatures(parameters={"x": 2}, trial_index=0),
        ]
        observations = recombine_observations(obs_features, obs_data, arm_names)
        modelbridge = Mock(
            status_quo=Mock(
                data=obs_data[0], features=obs_features[0], arm_name=arm_names[0]
            ),
            status_quo_data_by_trial={0: obs_data[0]},
        )
        tf = Relativize(
            search_space=None,
            observations=observations,
            modelbridge=modelbridge,
        )
        results = tf.transform_observations(observations)
        self.assertEqual(results[0].data.metric_names, metric_names)
        # status quo means must always be zero
        self.assertTrue(
            np.allclose(results[0].data.means, np.array([0.0, 0.0])),
            results[0].data.means,
        )
        # status quo covariances must always be zero
        self.assertTrue(
            np.allclose(results[0].data.covariance, np.array([[0.0, 0.0], [0.0, 0.0]])),
            results[0].data.covariance,
        )
        self.assertEqual(results[1].data.metric_names, metric_names)
        self.assertTrue(
            np.allclose(results[1].data.means, np.array([-51.25, 98.4])),
            results[1].data.means,
        )
        self.assertTrue(
            np.allclose(
                results[1].data.covariance, np.array([[812.5, 0.0], [0.0, 480.0]])
            ),
            results[1].data.covariance,
        )
        obsd_t = tf._untransform_observation_data(obs_data)
        self.assertEqual(obsd_t, obs_data)

    # pyre-fixme[56]: Pyre was not able to infer the type of argument
    #  `hypothesis.strategies.floats($parameter$min_value = - 10.000000,
    #  $parameter$max_value = 10.000000)` to decorator factory `hypothesis.given`.
    @given(
        st.floats(min_value=-10.0, max_value=10.0),
        st.floats(min_value=0, max_value=10.0),
        st.floats(min_value=-10.0, max_value=10.0),
        st.floats(min_value=0, max_value=10.0),
    )
    @settings(max_examples=1000, deadline=None)
    def test_transform_status_quos_always_zero(
        self,
        sq_mean: float,
        sq_sem: float,
        mean: float,
        sem: float,
    ) -> None:
        assume(abs(sq_mean) >= 1e-10)
        assume(abs(sq_mean) != sq_sem)

        arm_names = ["status_quo", "0_0"]
        obs_data = [
            ObservationData(
                metric_names=["foo"],
                means=np.array([sq_mean]),
                covariance=np.array([[sq_sem]]),
            ),
            ObservationData(
                metric_names=["foo"],
                means=np.array([mean]),
                covariance=np.array([[sem]]),
            ),
        ]
        obs_features = [
            # pyre-fixme[6]: For 2nd param expected `Optional[int64]` but got `int`.
            ObservationFeatures(parameters={"x": 1}, trial_index=0),
            # pyre-fixme[6]: For 2nd param expected `Optional[int64]` but got `int`.
            ObservationFeatures(parameters={"x": 2}, trial_index=0),
        ]
        modelbridge = Mock(
            status_quo=Mock(
                data=obs_data[0], features=obs_features[0], arm_name=arm_names[0]
            ),
            status_quo_data_by_trial={0: obs_data[0]},
        )
        observations = recombine_observations(obs_features, obs_data, arm_names)
        transform = Relativize(
            search_space=None,
            observations=observations,
            modelbridge=modelbridge,
        )
        relative_obs = transform.transform_observations(observations)
        self.assertEqual(relative_obs[0].data.metric_names, ["foo"])
        self.assertAlmostEqual(relative_obs[0].data.means[0], 0, places=4)
        self.assertAlmostEqual(relative_obs[0].data.covariance[0][0], 0, places=4)

    def test_multitask_data(self) -> None:
        experiment = get_branin_with_multi_task()
        data = experiment.fetch_data()

        observations = observations_from_data(
            experiment=experiment,
            data=data,
        )
        relative_observations = observations_from_data(
            experiment=experiment,
            data=relativize_data(
                data=data,
                status_quo_name="status_quo",
                as_percent=True,
                include_sq=True,
            ),
        )

        sq_obs_data = []
        for i in data.df["trial_index"].unique():
            status_quo_data = data.df.loc[
                (data.df["arm_name"] == "status_quo") & (data.df["trial_index"] == i)
            ]
            sq_obs_data.append(
                ObservationData(
                    metric_names=status_quo_data["metric_name"].to_numpy(),
                    means=status_quo_data["mean"].to_numpy(),
                    covariance=status_quo_data["sem"].to_numpy()[np.newaxis, :] ** 2,
                )
            )

        modelbridge = Mock(
            status_quo=Observation(
                data=sq_obs_data[0],
                features=ObservationFeatures(
                    parameters=not_none(experiment.status_quo).parameters
                ),
                arm_name="status_quo",
            ),
            status_quo_data_by_trial={
                i: sq_obs_data[i] for i in range(len(sq_obs_data))
            },
        )

        transform = Relativize(
            search_space=None,
            observations=observations,
            modelbridge=modelbridge,
        )

        relative_obs_t = transform.transform_observations(observations)
        self.maxDiff = None
        # this assertion just checks that order is the same, which
        # is only important for the purposes of this test
        self.assertEqual(
            [datum.data.metric_names for datum in relative_obs_t],
            [datum.data.metric_names for datum in relative_observations],
        )
        means = [
            np.array([datum.data.means for datum in relative_obs_t]),
            np.array([datum.data.means for datum in relative_observations]),
        ]
        # `self.assertAlmostEqual(relative_obs_data, expected_obs_data)`
        # fails 1% of the time, so we check with numpy.
        self.assertTrue(
            all(np.isclose(means[0], means[1])),
            means,
        )
        covariances = [
            np.array([datum.data.covariance for datum in relative_observations]),
            np.array([datum.data.covariance for datum in relative_obs_t]),
        ]
        self.assertTrue(
            all(np.isclose(covariances[0], covariances[1])),
            covariances,
        )


class RelativizeDataOptConfigTest(TestCase):
    def setUp(self) -> None:
        super().setUp()
        search_space = get_search_space()
        gr = Models.SOBOL(search_space=search_space).gen(n=1)
        self.model = Mock(
            search_space=search_space,
            status_quo=Mock(
                features=ObservationFeatures(parameters=gr.arms[0].parameters)
            ),
        )

    def test_transform_optimization_config_without_constraints(self) -> None:
        relativize = Relativize(
            search_space=None,
            observations=[],
            modelbridge=self.model,
        )
        optimization_config = get_branin_optimization_config()
        new_config = relativize.transform_optimization_config(
            optimization_config=optimization_config,
            modelbridge=None,
            fixed_features=Mock(),
        )
        self.assertEqual(new_config.objective, optimization_config.objective)

    def test_transform_optimization_config_with_relative_constraints(self) -> None:
        relativize = Relativize(
            search_space=None,
            observations=[],
            modelbridge=self.model,
        )
        optimization_config = get_branin_optimization_config()
        optimization_config.outcome_constraints = [
            OutcomeConstraint(
                metric=BraninMetric("b2", ["x2", "x1"]),
                op=ComparisonOp.GEQ,
                bound=-200.0,
                relative=True,
            )
        ]
        new_config = relativize.transform_optimization_config(
            optimization_config=deepcopy(optimization_config),
            modelbridge=None,
            fixed_features=Mock(),
        )
        self.assertEqual(new_config.objective, optimization_config.objective)
        self.assertEqual(
            new_config.outcome_constraints[0].bound,
            optimization_config.outcome_constraints[0].bound,
        )
        self.assertFalse(new_config.outcome_constraints[0].relative)
        # Untransform the constraints
        cons = relativize.untransform_outcome_constraints(
            outcome_constraints=new_config.outcome_constraints, fixed_features=Mock()
        )
        self.assertEqual(cons, optimization_config.outcome_constraints)

    def test_transform_optimization_config_with_non_relative_constraints(self) -> None:
        relativize = Relativize(
            search_space=None,
            observations=[],
            modelbridge=self.model,
        )
        optimization_config = get_branin_optimization_config()
        optimization_config.outcome_constraints = [
            OutcomeConstraint(
                metric=BraninMetric("b2", ["x2", "x1"]),
                op=ComparisonOp.GEQ,
                bound=-200.0,
                relative=False,
            )
        ]
        with self.assertRaisesRegex(ValueError, "All constraints must be relative"):
            relativize.transform_optimization_config(
                optimization_config=optimization_config,
                modelbridge=None,
                fixed_features=Mock(),
            )

    def test_transform_optimization_config_with_relative_thresholds(self) -> None:
        relativize = Relativize(
            search_space=None,
            observations=[],
            modelbridge=self.model,
        )
        optimization_config = get_branin_multi_objective_optimization_config(
            has_objective_thresholds=True,
        )
        for threshold in optimization_config.objective_thresholds:
            threshold.relative = True

        new_config = relativize.transform_optimization_config(
            optimization_config=optimization_config,
            modelbridge=None,
            fixed_features=Mock(),
        )
        self.assertEqual(new_config.objective, optimization_config.objective)
        self.assertEqual(
            # pyre-fixme[16]: `OptimizationConfig` has no attribute
            #  `objective_thresholds`.
            new_config.objective_thresholds[0].bound,
            optimization_config.objective_thresholds[0].bound,
        )
        self.assertFalse(new_config.objective_thresholds[0].relative)
        self.assertEqual(
            new_config.objective_thresholds[1].bound,
            optimization_config.objective_thresholds[1].bound,
        )
        self.assertFalse(new_config.objective_thresholds[1].relative)

    def test_transform_optimization_config_with_non_relative_thresholds(self) -> None:
        relativize = Relativize(
            search_space=None,
            observations=[],
            modelbridge=self.model,
        )
        optimization_config = get_branin_multi_objective_optimization_config(
            has_objective_thresholds=True,
        )
        optimization_config.objective_thresholds[1].relative = False

        with self.assertRaisesRegex(
            ValueError, "All objective thresholds must be relative"
        ):
            relativize.transform_optimization_config(
                optimization_config=optimization_config,
                modelbridge=None,
                fixed_features=Mock(),
            )
