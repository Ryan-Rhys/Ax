#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

from collections import defaultdict

from dataclasses import dataclass, field
from logging import Logger
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

# Module-level import to avoid circular dependency b/w this file and
# generation_strategy.py
from ax import modelbridge
from ax.core.arm import Arm
from ax.core.base_trial import TrialStatus
from ax.core.data import Data
from ax.core.experiment import Experiment
from ax.core.generator_run import GeneratorRun
from ax.core.observation import ObservationFeatures
from ax.core.optimization_config import OptimizationConfig
from ax.core.search_space import SearchSpace
from ax.exceptions.core import NoDataError, UserInputError

from ax.exceptions.generation_strategy import (
    GenerationStrategyRepeatedPoints,
    MaxParallelismReachedException,
)
from ax.modelbridge.base import ModelBridge
from ax.modelbridge.completion_criterion import CompletionCriterion
from ax.modelbridge.cross_validation import BestModelSelector, CVDiagnostics, CVResult
from ax.modelbridge.model_spec import FactoryFunctionModelSpec, ModelSpec
from ax.modelbridge.registry import ModelRegistryBase
from ax.utils.common.base import Base, SortableBase
from ax.utils.common.logger import get_logger
from ax.utils.common.typeutils import not_none


logger: Logger = get_logger(__name__)

TModelFactory = Callable[..., ModelBridge]
CANNOT_SELECT_ONE_MODEL_MSG = """
Base `GenerationNode` does not implement selection among fitted
models, so exactly one `ModelSpec` must be specified when using
`GenerationNode._pick_fitted_model_to_gen_from` (usually called
by `GenerationNode.gen`.
"""
MAX_GEN_DRAWS = 5
MAX_GEN_DRAWS_EXCEEDED_MESSAGE = (
    f"GenerationStrategy exceeded `MAX_GEN_DRAWS` of {MAX_GEN_DRAWS} while trying to "
    "generate a unique parameterization. This indicates that the search space has "
    "likely been fully explored, or that the sweep has converged."
)


class GenerationNode:
    """Base class for generation node, capable of fitting one or more
    model specs under the hood and generating candidates from them.
    """

    model_specs: List[ModelSpec]
    should_deduplicate: bool
    _model_spec_to_gen_from: Optional[ModelSpec] = None

    def __init__(
        self,
        model_specs: List[ModelSpec],
        best_model_selector: Optional[BestModelSelector] = None,
        should_deduplicate: bool = False,
    ) -> None:
        # While `GenerationNode` only handles a single `ModelSpec` in the `gen`
        # and `_pick_fitted_model_to_gen_from` methods, we validate the
        # length of `model_specs` in `_pick_fitted_model_to_gen_from` in order
        # to not require all `GenerationNode` subclasses to override an `__init__`
        # method to bypass that validation.
        self.model_specs = model_specs
        self.best_model_selector = best_model_selector
        self.should_deduplicate = should_deduplicate

    @property
    def model_spec_to_gen_from(self) -> ModelSpec:
        """Returns the cached `_model_spec_to_gen_from` or gets it from
        `_pick_fitted_model_to_gen_from` and then caches and returns it
        """
        if self._model_spec_to_gen_from is not None:
            return self._model_spec_to_gen_from

        self._model_spec_to_gen_from = self._pick_fitted_model_to_gen_from()
        return self._model_spec_to_gen_from

    @property
    def model_enum(self) -> ModelRegistryBase:
        """model_enum from self.model_spec_to_gen_from for convenience"""
        return self.model_spec_to_gen_from.model_enum

    @property
    def model_kwargs(self) -> Optional[Dict[str, Any]]:
        """model_kwargs from self.model_spec_to_gen_from for convenience"""
        return self.model_spec_to_gen_from.model_kwargs

    @property
    def model_gen_kwargs(self) -> Optional[Dict[str, Any]]:
        """model_gen_kwargs from self.model_spec_to_gen_from for convenience"""
        return self.model_spec_to_gen_from.model_gen_kwargs

    @property
    def model_cv_kwargs(self) -> Optional[Dict[str, Any]]:
        """model_cv_kwargs from self.model_spec_to_gen_from for convenience"""
        return self.model_spec_to_gen_from.model_cv_kwargs

    @property
    def fitted_model(self) -> ModelBridge:
        """fitted_model from self.model_spec_to_gen_from for convenience"""
        return self.model_spec_to_gen_from.fitted_model

    @property
    def fixed_features(self) -> Optional[ObservationFeatures]:
        """fixed_features from self.model_spec_to_gen_from for convenience"""
        if len({model_spec.fixed_features for model_spec in self.model_specs}) == 1:
            return self.model_specs[0].fixed_features

        return self.model_spec_to_gen_from.fixed_features

    @property
    def cv_results(self) -> Optional[List[CVResult]]:
        """cv_results from self.model_spec_to_gen_from for convenience"""
        return self.model_spec_to_gen_from.cv_results

    @property
    def diagnostics(self) -> Optional[CVDiagnostics]:
        """diagnostics from self.model_spec_to_gen_from for convenience"""
        return self.model_spec_to_gen_from.diagnostics

    def fit(
        self,
        experiment: Experiment,
        data: Data,
        search_space: Optional[SearchSpace] = None,
        optimization_config: Optional[OptimizationConfig] = None,
        **kwargs: Any,
    ) -> None:
        """Fits the specified models to the given experiment + data using
        the model kwargs set on each corresponding model spec and the kwargs
        passed to this method.

        NOTE: Local kwargs take precedence over the ones stored in
        ``ModelSpec.model_kwargs``.
        """
        self._model_spec_to_gen_from = None
        for model_spec in self.model_specs:
            model_spec.fit(  # Stores the fitted model as `model_spec._fitted_model`
                experiment=experiment,
                data=data,
                search_space=search_space,
                optimization_config=optimization_config,
                **kwargs,
            )

    def update(self, experiment: Experiment, new_data: Data) -> None:
        """Updates the specified models on the given experiment + new data."""
        raise NotImplementedError("`update` is not supported yet.")

    def gen(
        self,
        n: Optional[int] = None,
        pending_observations: Optional[Dict[str, List[ObservationFeatures]]] = None,
        max_gen_draws_for_deduplication: int = MAX_GEN_DRAWS,
        arms_by_signature_for_deduplication: Optional[Dict[str, Arm]] = None,
        **model_gen_kwargs: Any,
    ) -> GeneratorRun:
        """Picks a fitted model, from which to generate candidates (via
        ``self._pick_fitted_model_to_gen_from``) and generates candidates
        from it. Uses the ``model_gen_kwargs`` set on the selected ``ModelSpec``
        alongside any kwargs passed in to this function (with local kwargs)
        taking precedent.

        Args:
            n: Optional nteger representing how many arms should be in the generator
                run produced by this method. When this is ``None``, ``n`` will be
                determined by the ``ModelSpec`` that we are generating from.
            pending_observations: A map from metric name to pending
                observations for that metric, used by some models to avoid
                resuggesting points that are currently being evaluated.
            max_gen_draws_for_deduplication: TODO
            model_gen_kwargs: Keyword arguments, passed through to ``ModelSpec.gen``;
                these override any pre-specified in ``ModelSpec.model_gen_kwargs``.

        NOTE: Models must have been fit prior to calling ``gen``.
        NOTE: Some underlying models may ignore the ``n`` argument and produce a
            model-determined number of arms. In that case this method will also output
            a generator run with number of arms (that can differ from ``n``).
        """
        model_spec = self.model_spec_to_gen_from
        should_generate_run = True
        generator_run = None
        n_gen_draws = 0
        # Keep generating until each of `generator_run.arms` is not a duplicate
        # of a previous arm, if `should_deduplicate is True`
        while should_generate_run:
            generator_run = model_spec.gen(
                # If `n` is not specified, ensure that the `None` value does not
                # override the one set in `model_spec.model_gen_kwargs`.
                n=model_spec.model_gen_kwargs.get("n")
                if n is None and model_spec.model_gen_kwargs
                else n,
                # For `pending_observations`, prefer the input to this function, as
                # `pending_observations` are dynamic throughout the experiment and thus
                # unlikely to be specified in `model_spec.model_gen_kwargs`.
                pending_observations=pending_observations,
                **model_gen_kwargs,
            )

            should_generate_run = (
                self.should_deduplicate
                and arms_by_signature_for_deduplication
                and any(
                    arm.signature in arms_by_signature_for_deduplication
                    for arm in generator_run.arms
                )
            )
            n_gen_draws += 1
            if should_generate_run:
                if n_gen_draws > max_gen_draws_for_deduplication:
                    raise GenerationStrategyRepeatedPoints(
                        MAX_GEN_DRAWS_EXCEEDED_MESSAGE
                    )
                else:
                    logger.info(
                        "The generator run produced duplicate arms. Re-running the "
                        "generation step in an attempt to deduplicate. Candidates "
                        f"produced in the last generator run: {generator_run.arms}."
                    )

        return not_none(generator_run)

    def _pick_fitted_model_to_gen_from(self) -> ModelSpec:
        """Select one model to generate from among the fitted models on this
        generation node.

        NOTE: In base ``GenerationNode`` class, this method does the following:
          1. if this ``GenerationNode`` has an associated ``BestModelSelector``,
             use it to select one model to generate from among the fitted models
             on this generation node.
          2. otherwise, ensure that this ``GenerationNode`` only contains one
             `ModelSpec` and select it.
        """
        if self.best_model_selector is None:
            if len(self.model_specs) != 1:
                raise NotImplementedError(CANNOT_SELECT_ONE_MODEL_MSG)
            return self.model_specs[0]

        for model_spec in self.model_specs:
            model_spec.cross_validate()

        best_model_index = not_none(self.best_model_selector).best_diagnostic(
            diagnostics=[not_none(m.diagnostics) for m in self.model_specs],
        )
        return self.model_specs[best_model_index]

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(model_specs={self.model_specs})"


@dataclass
class GenerationStep(GenerationNode, SortableBase):
    """One step in the generation strategy, corresponds to a single model.
    Describes the model, how many trials will be generated with this model, what
    minimum number of observations is required to proceed to the next model, etc.

    NOTE: Model can be specified either from the model registry
    (`ax.modelbridge.registry.Models` or using a callable model constructor. Only
    models from the registry can be saved, and thus optimization can only be
    resumed if interrupted when using models from the registry.

    Args:
        model: A member of `Models` enum or a callable returning an instance of
            `ModelBridge` with an instantiated underlying `Model`. Refer to
            `ax/modelbridge/factory.py` for examples of such callables.
        num_trials: How many trials to generate with the model from this step.
            If set to -1, trials will continue to be generated from this model
            as long as `generation_strategy.gen` is called (available only for
            the last of the generation steps).
        min_trials_observed: How many trials must be completed before the
            generation strategy can proceed to the next step. Defaults to 0.
            If `num_trials` of a given step have been generated but `min_trials_
            observed` have not been completed, a call to `generation_strategy.gen`
            will fail with a `DataRequiredError`.
        max_parallelism: How many trials generated in the course of this step are
            allowed to be run (i.e. have `trial.status` of `RUNNING`) simultaneously.
            If `max_parallelism` trials from this step are already running, a call
            to `generation_strategy.gen` will fail with a `MaxParallelismReached
            Exception`, indicating that more trials need to be completed before
            generating and running next trials.
        use_update: Whether to use `model_bridge.update` instead or reinstantiating
            model + bridge on every call to `gen` within a single generation step.
            NOTE: use of `update` on stateful models that do not implement `_get_state`
            may result in inability to correctly resume a generation strategy from
            a serialized state.
        enforce_num_trials: Whether to enforce that only `num_trials` are generated
            from the given step. If False and `num_trials` have been generated, but
            `min_trials_observed` have not been completed, `generation_strategy.gen`
            will continue generating trials from the current step, exceeding `num_
            trials` for it. Allows to avoid `DataRequiredError`, but delays
            proceeding to next generation step.
        model_kwargs: Dictionary of kwargs to pass into the model constructor on
            instantiation. E.g. if `model` is `Models.SOBOL`, kwargs will be applied
            as `Models.SOBOL(**model_kwargs)`; if `model` is `get_sobol`, `get_sobol(
            **model_kwargs)`. NOTE: if generation strategy is interrupted and
            resumed from a stored snapshot and its last used model has state saved on
            its generator runs, `model_kwargs` is updated with the state dict of the
            model, retrieved from the last generator run of this generation strategy.
        model_gen_kwargs: Each call to `generation_strategy.gen` performs a call to the
            step's model's `gen` under the hood; `model_gen_kwargs` will be passed to
            the model's `gen` like so: `model.gen(**model_gen_kwargs)`.
        completion_criteria: List of CompletionCriterion. All `is_met` must evaluate
            True for the GenerationStrategy to move on to the next Step
        index: Index of this generation step, for use internally in `Generation
            Strategy`. Do not assign as it will be reassigned when instantiating
            `GenerationStrategy` with a list of its steps.
        should_deduplicate: Whether to deduplicate the parameters of proposed arms
            against those of previous arms via rejection sampling. If this is True,
            the generation strategy will discard generator runs produced from the
            generation step that has `should_deduplicate=True` if they contain arms
            already present on the experiment and replace them with new generator runs.
            If no generator run with entirely unique arms could be produced in 5
            attempts, a `GenerationStrategyRepeatedPoints` error will be raised, as we
            assume that the optimization converged when the model can no longer suggest
            unique arms.

    Note for developers: by "model" here we really mean an Ax ModelBridge object, which
    contains an Ax Model under the hood. We call it "model" here to simplify and focus
    on explaining the logic of GenerationStep and GenerationStrategy.
    """

    # Required options:
    model: Union[ModelRegistryBase, Callable[..., ModelBridge]]
    num_trials: int

    # Optional model specifications:
    # Kwargs to pass into the Models constructor (or factory function).
    model_kwargs: Optional[Dict[str, Any]] = None
    # Kwargs to pass into the Model's `.gen` function.
    model_gen_kwargs: Optional[Dict[str, Any]] = None

    # Optional specifications for use in generation strategy:
    completion_criteria: Sequence[CompletionCriterion] = field(default_factory=list)
    min_trials_observed: int = 0
    max_parallelism: Optional[int] = None
    use_update: bool = False
    enforce_num_trials: bool = True
    # Whether the generation strategy should deduplicate the suggested arms against
    # the arms already present on the experiment. If this is `True`
    # on a given generation step, during that step the generation
    # strategy will discard a generator run that contains an arm
    # already present on the experiment and produce a new generator
    # run instead before returning it from `gen` or `_gen_multiple`.
    should_deduplicate: bool = False
    index: int = -1  # Index of this step, set internally.

    # Optional model name. Defaults to `model_spec.model_key`.
    model_name: str = field(default_factory=str)
    # [TODO] Handle experiment passing more eloquently by enforcing experiment
    # attribute is set in generation strategies class
    _generation_strategy: Optional[
        modelbridge.generation_strategy.GenerationStrategy
    ] = None

    def __post_init__(self) -> None:
        if (
            self.enforce_num_trials
            and (self.num_trials >= 0)
            and (self.min_trials_observed > self.num_trials)
        ):
            raise UserInputError(
                "`GenerationStep` received `min_trials_observed > num_trials` "
                f"(`min_trials_observed = {self.min_trials_observed}`, `num_trials = "
                f"{self.num_trials}`), making completion of this step impossible. "
                "Please alter inputs so that `min_trials_observed <= num_trials`."
            )
        if not isinstance(self.model, ModelRegistryBase):
            if not callable(self.model):
                raise UserInputError(
                    "`model` in generation step must be either a `ModelRegistryBase` "
                    "enum subclass entry or a callable factory function returning a "
                    "model bridge instance."
                )
            model_spec = FactoryFunctionModelSpec(
                factory_function=self.model,
                model_kwargs=self.model_kwargs,
                model_gen_kwargs=self.model_gen_kwargs,
            )
        else:
            model_spec = ModelSpec(
                model_enum=self.model,
                model_kwargs=self.model_kwargs,
                model_gen_kwargs=self.model_gen_kwargs,
            )
        if self.model_name == "":
            try:
                self.model_name = model_spec.model_key
            except TypeError:
                # Factory functions may not always have a model key defined.
                self.model_name = f"Unknown {model_spec.__class__.__name__}"
        super().__init__(
            model_specs=[model_spec], should_deduplicate=self.should_deduplicate
        )

    @property
    def model_spec(self) -> ModelSpec:
        return self.model_specs[0]

    @property
    def _unique_id(self) -> str:
        return str(self.index)

    @property
    def generation_strategy(self) -> modelbridge.generation_strategy.GenerationStrategy:
        if self._generation_strategy is None:
            raise ValueError(
                "Generation strategy has not been initialized on this step."
            )
        return not_none(self._generation_strategy)

    @property
    def experiment(self) -> Experiment:
        return self.generation_strategy.experiment

    def gen(
        self,
        n: Optional[int] = None,
        pending_observations: Optional[Dict[str, List[ObservationFeatures]]] = None,
        max_gen_draws_for_deduplication: int = MAX_GEN_DRAWS,
        arms_by_signature_for_deduplication: Optional[Dict[str, Arm]] = None,
        **model_gen_kwargs: Any,
    ) -> GeneratorRun:
        gr = super().gen(
            n=n,
            pending_observations=pending_observations,
            max_gen_draws_for_deduplication=max_gen_draws_for_deduplication,
            arms_by_signature_for_deduplication=arms_by_signature_for_deduplication,
            **model_gen_kwargs,
        )
        gr._generation_step_index = self.index
        return gr

    def __eq__(self, other: Base) -> bool:
        # We need to override `__eq__` to make sure we inherit the one from
        # the base class and not the one from dataclasses library, since we
        # want to be comparing equality of generation steps in the same way
        # as we compare equality of other Ax objects (and we want all the
        # same special-casing to apply).
        return SortableBase.__eq__(self, other=other)

    @property
    def trial_indices(self) -> Dict[int, Set[int]]:
        """A mapping from generation step index to the trials of the experiment
        associated with that GenerationStep. NOTE: This maps all generation steps
        up to and including the current generation step.
        """
        trial_indices_by_step = defaultdict(set)

        for trial_index, trial in self.experiment.trials.items():
            if (
                trial._generation_step_index is not None
                and trial._generation_step_index <= self.index
            ):
                trial_indices_by_step[trial._generation_step_index].add(trial_index)

        return trial_indices_by_step

    @property
    def num_can_complete(self) -> int:
        """Number of trials in generation strategy that can
        be completed (so are not in status `FAILED` or `ABANDONED`). Used to keep
        track of how many generator runs (that become trials) can be produced
        from this generation step.

        NOTE: This includes `COMPLETED` trials.
        """
        step_trials = self.trial_indices[self.index]
        by_status = self.experiment.trial_indices_by_status

        # Number of trials that will not be `COMPLETED`, used to avoid counting
        # unsuccessfully terminated trials against the number of generated trials
        # during determination of whether enough trials have been generated and
        # completed to proceed to the next generation step.
        num_will_not_complete = len(
            step_trials.intersection(
                by_status[TrialStatus.FAILED].union(by_status[TrialStatus.ABANDONED])
            )
        )
        return len(step_trials) - num_will_not_complete

    @property
    def _num_completed(self) -> int:
        """Number of trials in status `COMPLETED` or `EARLY_STOPPED` for
        this generation step of this strategy. We include early
        stopped trials because their data will be used in the model,
        so they are completed from the model's point of view and should
        count towards that total.
        """
        step_trials = self.trial_indices[self.index]
        by_status = self.experiment.trial_indices_by_status

        return len(
            step_trials.intersection(
                by_status[TrialStatus.COMPLETED].union(
                    by_status[TrialStatus.EARLY_STOPPED]
                )
            )
        )

    def num_trials_to_gen_and_complete(
        self,
    ) -> Tuple[int, int]:
        """Returns how many generator runs (to be made into a trial each) are left to
        generate in this step and how many are left to be completed in it before
        the generation strategy can move to the next step.

        NOTE: returns (-1, -1) if the number of trials to be generated from the given
        step is unlimited (and therefore it must be the last generation step).
        """
        if self.num_trials == -1:
            return -1, -1

        # More than `num_trials` can be generated (if not `enforce_num_trials=False`)
        # and more than `min_trials_observed` can be completed (if `min_trials_observed
        # < `num_trials`), so `left_to_gen` and `left_to_complete` should be clamped
        # to lower bound of 0.
        left_to_gen = max(self.num_trials - self.num_can_complete, 0)
        left_to_complete = max(self.min_trials_observed - self._num_completed, 0)
        return left_to_gen, left_to_complete

    def num_remaining_trials_until_max_parallelism(
        self, raise_max_parallelism_reached_exception: bool = True
    ) -> Optional[int]:
        """Returns how many generator runs (to be made into a trial each) are left to
        generate before the `max_parallelism` limit is reached for this generation step.

        Args:
            raise_max_parallelism_reached_exception: Whether to raise
                ``MaxParallelismReachedException`` if number of trials running in
                this generation step exceeds maximum parallelism for it.
        """
        max_parallelism = self.max_parallelism
        num_running = self.num_running_trials

        if max_parallelism is None:
            return None  # There was no `max_parallelism` limit.

        if raise_max_parallelism_reached_exception and num_running >= max_parallelism:
            raise MaxParallelismReachedException(
                step_index=self.index,
                model_name=self.model_name,
                num_running=num_running,
            )

        return max_parallelism - num_running

    @property
    def num_running_trials(self) -> int:
        """Number of trials in status `RUNNING` for this generation step of this
        strategy.
        """
        num_running = 0
        for trial in self.experiment.trials.values():
            if trial._generation_step_index == self.index and trial.status.is_running:
                num_running += 1
        return num_running

    def get_data_for_fit(
        self, passed_in_data: Optional[Data], previous_step_required_observations: bool
    ) -> Data:
        """
        Correctly fetches data given this generation step's configuration, and checks
        for invalid data states before returning it.

        Args:
            passed_in_data: An optional provided aata object for fitting the model
                for this generation step
            previous_step_required_observations: Information about the previous
                generation step used to check and raise an error if we are in an invalid
                state due to unexpected empty data

        Returns:
            Data: Data for fitting a model to generate this generation step
        """
        if passed_in_data is None:
            if self.use_update:
                # If this step is using `update`, it's important to instantiate
                # the model with data for completed trials only, so later we can
                # update it with data for new trials as they become completed.
                # `experiment.lookup_data` can lookup all available data, including
                # for non-completed trials (depending on how the experiment's metrics
                # implement `fetch_experiment_data`). We avoid fetching data for
                # trials with statuses other than `COMPLETED`, by fetching specifically
                # for `COMPLETED` trials.
                avail_while_running_metrics = {
                    m.name
                    for m in self.experiment.metrics.values()
                    if m.is_available_while_running()
                }
                if avail_while_running_metrics:
                    raise NotImplementedError(
                        f"Metrics {avail_while_running_metrics} are available while "
                        "trial is running, but use of `update` functionality in "
                        "generation step relies on new data being available upon "
                        "trial completion."
                    )
                data = self.experiment.lookup_data(
                    trial_indices=self.experiment.trial_indices_by_status[
                        TrialStatus.COMPLETED
                    ]
                )
            else:
                data = self.experiment.lookup_data()
        else:
            data = passed_in_data
        # If previous step required observed data, we should raise an error even if
        # enough trials were completed. Such an empty data case does indicate an
        # invalid state; this check is to improve the experience of detecting and
        # debugging the invalid state that led to this.
        if data.df.empty and previous_step_required_observations:
            raise NoDataError(
                f"Observed data is required for generation step #{self.index} "
                f"(model {self.model_name}), but fetched data was empty. "
                "Something is wrong with experiment setup -- likely metrics do not "
                "implement fetching logic (check your metrics) or no data was "
                "attached to experiment for completed trials."
            )
        return data
