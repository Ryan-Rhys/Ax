#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

from copy import deepcopy

from logging import Logger
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd
from ax.core.base_trial import BaseTrial, TrialStatus
from ax.core.data import Data
from ax.core.experiment import Experiment
from ax.core.generator_run import GeneratorRun
from ax.core.observation import ObservationFeatures
from ax.exceptions.core import DataRequiredError, UserInputError
from ax.exceptions.generation_strategy import GenerationStrategyCompleted

from ax.modelbridge.base import ModelBridge
from ax.modelbridge.generation_node import GenerationStep
from ax.modelbridge.modelbridge_utils import extend_pending_observations
from ax.modelbridge.registry import _extract_model_state_after_gen, ModelRegistryBase
from ax.utils.common.base import Base
from ax.utils.common.logger import _round_floats_for_logging, get_logger
from ax.utils.common.typeutils import checked_cast, not_none

logger: Logger = get_logger(__name__)


MAX_CONDITIONS_GENERATED = 10000
MAX_GEN_DRAWS = 5
MAX_GEN_DRAWS_EXCEEDED_MESSAGE = (
    f"GenerationStrategy exceeded `MAX_GEN_DRAWS` of {MAX_GEN_DRAWS} while trying to "
    "generate a unique parameterization. This indicates that the search space has "
    "likely been fully explored, or that the sweep has converged."
)


class GenerationStrategy(Base):
    """GenerationStrategy describes which model should be used to generate new
    points for which trials, enabling and automating use of different models
    throughout the optimization process. For instance, it allows to use one
    model for the initialization trials, and another one for all subsequent
    trials. In the general case, this allows to automate use of an arbitrary
    number of models to generate an arbitrary numbers of trials
    described in the `trials_per_model` argument.

    Args:
        steps: A list of `GenerationStep` describing steps of this strategy.
        name: An optional name for this generaiton strategy. If not specified,
            strategy's name will be names of its steps' models joined with '+'.
    """

    _name: Optional[str]
    _steps: List[GenerationStep]
    _curr: GenerationStep  # Current step in the strategy.
    # Whether all models in this GS are in Models registry enum.
    _uses_registered_models: bool
    # All generator runs created through this generation strategy, in chronological
    # order.
    _generator_runs: List[GeneratorRun]
    # Experiment, for which this generation strategy has generated trials, if
    # it exists.
    _experiment: Optional[Experiment] = None
    # Trial indices as last seen by the model; updated in `_model` property setter.
    # pyre-fixme[4]: Attribute must be annotated.
    _seen_trial_indices_by_status = None
    _model: Optional[ModelBridge] = None  # Current model.

    def __init__(self, steps: List[GenerationStep], name: Optional[str] = None) -> None:
        assert isinstance(steps, list) and all(
            isinstance(s, GenerationStep) for s in steps
        ), "Steps must be a GenerationStep list."
        self._name = name
        self._steps = steps
        self._uses_registered_models = True
        self._generator_runs = []
        for idx, step in enumerate(self._steps):
            if step.num_trials == -1 and len(step.completion_criteria) < 1:
                if idx < len(self._steps) - 1:
                    raise UserInputError(
                        "Only last step in generation strategy can have `num_trials` "
                        "set to -1 to indicate that the model in the step should "
                        "be used to generate new trials indefinitely unless "
                        "completion critera present."
                    )
            elif step.num_trials < 1 and step.num_trials != -1:
                raise UserInputError(
                    "`num_trials` must be positive or -1 (indicating unlimited) "
                    "for all generation steps."
                )
            if step.max_parallelism is not None and step.max_parallelism < 1:
                raise UserInputError(
                    "Maximum parallelism should be None (if no limit) or a positive"
                    f" number. Got: {step.max_parallelism} for step {step.model_name}."
                )
            step.index = idx
            step._generation_strategy = self
            if not isinstance(step.model, ModelRegistryBase):
                self._uses_registered_models = False
        if not self._uses_registered_models:
            logger.info(
                "Using model via callable function, "
                "so optimization is not resumable if interrupted."
            )
        self._curr = steps[0]
        self._seen_trial_indices_by_status = None

    @property
    def name(self) -> str:
        """Name of this generation strategy. Defaults to a combination of model
        names provided in generation steps.
        """
        if self._name is not None:
            return not_none(self._name)

        factory_names = (step.model_name for step in self._steps)
        # Trim the "get_" beginning of the factory function if it's there.
        factory_names = (n[4:] if n[:4] == "get_" else n for n in factory_names)
        self._name = "+".join(factory_names)
        return not_none(self._name)

    @name.setter
    def name(self, name: str) -> None:
        """Set generation strategy name."""
        self._name = name

    @property
    def model_transitions(self) -> List[int]:
        """List of trial indices where a transition happened from one model to
        another."""
        gen_changes = [step.num_trials for step in self._steps]
        return [sum(gen_changes[: i + 1]) for i in range(len(gen_changes))][:-1]

    @property
    def current_step(self) -> GenerationStep:
        """Current generation step."""
        return self._curr

    @property
    def model(self) -> Optional[ModelBridge]:
        """Current model in this strategy. Returns None if no model has been set
        yet (i.e., if no generator runs have been produced from this GS).
        """
        return self._curr.model_spec._fitted_model

    @property
    def experiment(self) -> Experiment:
        """Experiment, currently set on this generation strategy."""
        if self._experiment is None:
            raise ValueError("No experiment set on generation strategy.")
        return not_none(self._experiment)

    @experiment.setter
    def experiment(self, experiment: Experiment) -> None:
        """If there is an experiment set on this generation strategy as the
        experiment it has been generating generator runs for, check if the
        experiment passed in is the same as the one saved and log an information
        statement if its not. Set the new experiment on this generation strategy.
        """
        if self._experiment is None or experiment._name == self.experiment._name:
            self._experiment = experiment
        else:
            raise ValueError(
                "This generation strategy has been used for experiment "
                f"{self.experiment._name} so far; cannot reset experiment"
                f" to {experiment._name}. If this is a new optimization, "
                "a new generation strategy should be created instead."
            )

    @property
    def uses_non_registered_models(self) -> bool:
        """Whether this generation strategy involves models that are not
        registered and therefore cannot be stored."""
        return not self._uses_registered_models

    @property
    def last_generator_run(self) -> Optional[GeneratorRun]:
        """Latest generator run produced by this generation strategy.
        Returns None if no generator runs have been produced yet.
        """
        # Used to restore current model when decoding a serialized GS.
        return self._generator_runs[-1] if self._generator_runs else None

    @property
    def trials_as_df(self) -> Optional[pd.DataFrame]:
        """Puts information on individual trials into a data frame for easy
        viewing. For example:
        Gen. Step | Model | Trial Index | Trial Status | Arm Parameterizations
        0         | Sobol | 0           | RUNNING      | {"0_0":{"x":9.17...}}
        """
        logger.info(
            "Note that parameter values in dataframe are rounded to 2 decimal "
            "points; the values in the dataframe are thus not the exact ones "
            "suggested by Ax in trials."
        )

        if self._experiment is None or all(
            len(trials) == 0
            for step in self._steps
            for trials in step.trial_indices.values()
        ):
            return None
        records = [
            {
                "Generation Step": step.index,
                "Generation Model": self._steps[step.index].model_name,
                "Trial Index": trial_idx,
                "Trial Status": self.experiment.trials[trial_idx].status.name,
                "Arm Parameterizations": {
                    arm.name: _round_floats_for_logging(arm.parameters)
                    for arm in self.experiment.trials[trial_idx].arms
                },
            }
            for step in self._steps
            for _, trials in step.trial_indices.items()
            for trial_idx in trials
        ]
        return pd.DataFrame.from_records(records).reindex(
            columns=[
                "Generation Step",
                "Generation Model",
                "Trial Index",
                "Trial Status",
                "Arm Parameterizations",
            ]
        )

    def gen(
        self,
        experiment: Experiment,
        data: Optional[Data] = None,
        n: int = 1,
        pending_observations: Optional[Dict[str, List[ObservationFeatures]]] = None,
        **kwargs: Any,
    ) -> GeneratorRun:
        """Produce the next points in the experiment. Additional kwargs passed to
        this method are propagated directly to the underlying model's `gen`, along
        with the `model_gen_kwargs` set on the current generation step.

        NOTE: Each generator run returned from this function must become a single
        trial on the experiment to comply with assumptions made in generation
        strategy. Do not split one generator run produced from generation strategy
        into multiple trials (never making a generator run into a trial is allowed).

        Args:
            experiment: Experiment, for which the generation strategy is producing
                a new generator run in the course of `gen`, and to which that
                generator run will be added as trial(s). Information stored on the
                experiment (e.g., trial statuses) is used to determine which model
                will be used to produce the generator run returned from this method.
            data: Optional data to be passed to the underlying model's `gen`, which
                is called within this method and actually produces the resulting
                generator run. By default, data is all data on the `experiment` if
                `use_update` is False and only the new data since the last call to
                this method if `use_update` is True.
            n: Integer representing how many arms should be in the generator run
                produced by this method. NOTE: Some underlying models may ignore
                the `n` and produce a model-determined number of arms. In that
                case this method will also output a generator run with number of
                arms that can differ from `n`.
            pending_observations: A map from metric name to pending
                observations for that metric, used by some models to avoid
                resuggesting points that are currently being evaluated.
        """
        return self._gen_multiple(
            experiment=experiment,
            num_generator_runs=1,
            data=data,
            n=n,
            pending_observations=pending_observations,
            **kwargs,
        )[0]

    def current_generator_run_limit(
        self,
    ) -> Tuple[int, bool]:
        """How many generator runs can this generation strategy generate right now,
        assuming each one of them becomes its own trial, and whether optimization
        is completed.

        NOTE: This method might move the generation strategy to the next step, which
        is safe, as the next call to ``gen`` will just pick up from there.

        Returns: a two-item tuple of:
              - the number of generator runs that can currently be produced, with -1
                meaning unlimited generator runs,
              - whether optimization is completed and the generation strategy cannot
                generate any more generator runs at all.
        """
        try:
            self._maybe_move_to_next_step(raise_data_required_error=False)
        except GenerationStrategyCompleted:
            return 0, True

        to_gen = self._curr.num_trials_to_gen_and_complete()[0]
        if to_gen < -1:
            # `_num_trials_to_gen_and_complete_in_curr_step()` should return value
            # of -1 or greater always.
            raise RuntimeError(
                "Number of trials left to generate in current generation step is "
                f"{to_gen}. This is an unexpected state of the generation strategy."
            )

        until_max_parallelism = self._curr.num_remaining_trials_until_max_parallelism(
            raise_max_parallelism_reached_exception=False
        )

        # If there is no limitation on the number of trials in the step and
        # there is a parallelism limit, return number of trials until that limit.
        if until_max_parallelism is not None and to_gen == -1:
            return until_max_parallelism, False

        # If there is a limitation on the number of trials in the step and also on
        # parallelism, return the number of trials until either one of the limits.
        if until_max_parallelism is not None:  # NOTE: to_gen must be >= 0 here
            return min(to_gen, until_max_parallelism), False

        # If there is no limit on parallelism, return how many trials are left to
        # gen in this step (might be -1 indicating unlimited).
        return to_gen, False

    def clone_reset(self) -> GenerationStrategy:
        """Copy this generation strategy without it's state."""
        # TODO[drfreund]: unset `_generation_strategy`` from steps
        return GenerationStrategy(name=self.name, steps=deepcopy(self._steps))

    def _unset_non_persistent_state_fields(self) -> None:
        """Utility for testing convenience: unset fields of generation strategy
        that are set during candidate generation; these fields are not persisted
        during storage. To compare a pre-storage and a reloaded generation
        strategies; call this utility on the pre-storage one first. The rest
        of the fields should be identical.
        """
        self._seen_trial_indices_by_status = None
        self._model = None
        for s in self._steps:
            s._model_spec_to_gen_from = None

    def __repr__(self) -> str:
        """String representation of this generation strategy."""
        repr = f"GenerationStrategy(name='{self.name}', steps=["
        remaining_trials = "subsequent" if len(self._steps) > 1 else "all"
        for step in self._steps:
            num_trials = (
                f"{step.num_trials}" if step.num_trials != -1 else remaining_trials
            )
            try:
                model_name = step.model_name
            except TypeError:
                model_name = "model with unknown name"

            repr += f"{model_name} for {num_trials} trials, "
        repr = repr[:-2]
        repr += "])"
        return repr

    # ------------------------- Candidate generation helpers. -------------------------

    def _gen_multiple(
        self,
        experiment: Experiment,
        num_generator_runs: int,
        data: Optional[Data] = None,
        n: int = 1,
        pending_observations: Optional[Dict[str, List[ObservationFeatures]]] = None,
        **model_gen_kwargs: Any,
    ) -> List[GeneratorRun]:
        """Produce multiple generator runs at once, to be made into multiple
        trials on the experiment.

        NOTE: This is used to ensure that maximum paralellism and number
        of trials per step are not violated when producing many generator
        runs from this generation strategy in a row. Without this function,
        if one generates multiple generator runs without first making any
        of them into running trials, generation strategy cannot enforce that it only
        produces as many generator runs as are allowed by the paralellism
        limit and the limit on number of trials in current step.

        Args:
            experiment: Experiment, for which the generation strategy is producing
                a new generator run in the course of `gen`, and to which that
                generator run will be added as trial(s). Information stored on the
                experiment (e.g., trial statuses) is used to determine which model
                will be used to produce the generator run returned from this method.
            data: Optional data to be passed to the underlying model's `gen`, which
                is called within this method and actually produces the resulting
                generator run. By default, data is all data on the `experiment` if
                `use_update` is False and only the new data since the last call to
                this method if `use_update` is True.
            n: Integer representing how many arms should be in the generator run
                produced by this method. NOTE: Some underlying models may ignore
                the ``n`` and produce a model-determined number of arms. In that
                case this method will also output a generator run with number of
                arms that can differ from ``n``.
            pending_observations: A map from metric name to pending
                observations for that metric, used by some models to avoid
                resuggesting points that are currently being evaluated.
            model_gen_kwargs: Keyword arguments that are passed through to
                ``GenerationStep.gen``, which will pass them through to
                ``ModelSpec.gen``, which will pass them to ``ModelBridge.gen``.
        """
        self.experiment = experiment
        self._maybe_move_to_next_step()
        self._fit_or_update_current_model(data=data)

        # Make sure to not make too many generator runs and
        # exceed maximum allowed paralellism for the step.
        num_until_max_parallelism = (
            self._curr.num_remaining_trials_until_max_parallelism()
        )
        if num_until_max_parallelism is not None:
            num_generator_runs = min(num_generator_runs, num_until_max_parallelism)

        # Make sure not to extend number of trials expected in step.
        if self._curr.enforce_num_trials and self._curr.num_trials > 0:
            num_generator_runs = min(
                num_generator_runs,
                self._curr.num_trials - self._curr.num_can_complete,
            )

        generator_runs = []
        pending_observations = deepcopy(pending_observations) or {}
        for _ in range(num_generator_runs):
            try:
                generator_run = self._curr.gen(
                    n=n,
                    pending_observations=pending_observations,
                    arms_by_signature_for_deduplication=experiment.arms_by_signature,
                    **model_gen_kwargs,
                )

            except DataRequiredError as err:
                # Model needs more data, so we log the error and return
                # as many generator runs as we were able to produce, unless
                # no trials were produced at all (in which case its safe to raise).
                if len(generator_runs) == 0:
                    raise
                logger.debug(f"Model required more data: {err}.")
                break

            self._generator_runs.append(generator_run)
            generator_runs.append(generator_run)

            # Extend the `pending_observation` with newly generated point(s)
            # to avoid repeating them.
            extend_pending_observations(
                experiment=experiment,
                pending_observations=pending_observations,
                generator_run=generator_run,
            )
        return generator_runs

    # ------------------------- Model selection logic helpers. -------------------------

    def _fit_or_update_current_model(self, data: Optional[Data]) -> None:
        """Fits or update the model on the current generation step (does not move
        between generation steps).

        Args:
            data: Optional ``Data`` to fit or update with; if not specified, generation
                strategy will obtain the data via ``experiment.lookup_data``.
        """
        if self._model is not None and self._curr.use_update:
            new_data = self._get_data_for_update(passed_in_data=data)
            if new_data is not None:
                self._update_current_model(new_data=new_data)
        else:
            previous_step_req_observations = (
                self._curr.index > 0
                and self._steps[self._curr.index - 1].min_trials_observed > 0
            )
            self._fit_current_model(
                data=self._curr.get_data_for_fit(
                    passed_in_data=data,
                    previous_step_required_observations=previous_step_req_observations,
                )
            )
        self._save_seen_trial_indices()

    def _maybe_move_to_next_step(self, raise_data_required_error: bool = True) -> bool:
        """Moves this generation strategy to next step if conditions for moving are met.
        This method is safe to use both when generating candidates or simply checking
        how many generator runs (to be made into trials) can currently be produced.

        Conditions for moving to next step:
        1. ``num_trials`` in current generation step have been generated (generation
            strategy produced that many generator runs, which were then attached to
            trials),
        2. ``min_trials_observed`` in current generation step have been completed,
        3. current step is not the last in this generation strategy.


        NOTE: this method raises ``GenerationStrategyCompleted`` error if conditions 1
        and 2 above are met, but the current step is the last in generation strategy.
        It also raises ``DataRequiredError`` if all conditions below are true:
        1. ``raise_data_required_error`` argument is ``True``,
        2. ``num_trials`` in current generation step have been generated,
        3. ``min_trials_observed`` in current generation step have not been completed,
        4. ``enforce_num_trials`` in current generation step is ``True``.

        Args:
            raise_data_required_error: Whether to raise ``DataRequiredError`` in the
                case detailed above. Not raising the error is useful if just looking to
                check how many generator runs (to be made into trials) can be produced,
                but not actually producing them yet.

        Returns:
            Whether generation strategy moved to the next step.
        """
        to_gen, to_complete = self._curr.num_trials_to_gen_and_complete()
        if to_gen == to_complete == -1:  # Unlimited trials, check completion_criteria
            if len(self._curr.completion_criteria) > 0 and all(
                criterion.is_met(experiment=self.experiment)
                for criterion in self._curr.completion_criteria
            ):
                if len(self._steps) == self._curr.index + 1:
                    raise GenerationStrategyCompleted(
                        f"Generation strategy {self} generated all the trials as "
                        "specified in its steps."
                    )
                self._curr = self._steps[self._curr.index + 1]
                self._model = None
                return True

            return False

        enforcing_num_trials = self._curr.enforce_num_trials
        trials_left_to_gen = to_gen > 0
        trials_left_to_complete = to_complete > 0

        # If there is something left to gen or complete, we don't move to next step.
        if trials_left_to_gen or trials_left_to_complete:
            # Check that minimum observed_trials is satisfied if it's enforced.
            raise_error = raise_data_required_error
            if raise_error and enforcing_num_trials and not trials_left_to_gen:
                raise DataRequiredError(
                    "All trials for current model have been generated, but not enough "
                    "data has been observed to fit next model. Try again when more data"
                    " are available."
                )
            return False

        # If nothing left to gen or complete, move to next step if one is available.
        if len(self._steps) == self._curr.index + 1:
            raise GenerationStrategyCompleted(
                f"Generation strategy {self} generated all the trials as "
                "specified in its steps."
            )

        self._curr = self._steps[self._curr.index + 1]
        # Moving to the next step also entails unsetting this GS's model (since
        # new step's model will be initialized for the first time, so we don't
        # try to `update` it but rather initialize with all the data even if
        # `use_update` is true for the new generation step; this is done in
        # `self._fit_or_update_current_model).
        self._model = None
        return True

    def _fit_current_model(self, data: Data) -> None:
        """Instantiate the current model with all available data."""
        # If last generator run's index matches the current step, extract
        # model state from last generator run and pass it to the model
        # being instantiated in this function.
        lgr = self.last_generator_run
        # NOTE: This will not be easily compatible with `GenerationNode`;
        # will likely need to find last generator run per model. Not a problem
        # for now though as GS only allows `GenerationStep`-s for now.
        # Potential solution: store generator runs on `GenerationStep`-s and
        # split them per-model there.
        model_state_on_lgr = {}
        model_on_curr = self._curr.model
        if (
            lgr is not None
            and lgr._generation_step_index == self._curr.index
            and lgr._model_state_after_gen
        ):
            if self.model or isinstance(model_on_curr, ModelRegistryBase):
                # TODO[drfreund]: Consider moving this to `GenerationStep` or
                # `GenerationNode`.
                model_cls = (
                    self.model.model.__class__
                    if self.model is not None
                    # NOTE: This checked cast is save per the OR-statement in last line
                    # of the IF-check above.
                    else checked_cast(ModelRegistryBase, model_on_curr).model_class
                )
                model_state_on_lgr = _extract_model_state_after_gen(
                    generator_run=lgr,
                    model_class=model_cls,
                )
            else:
                logger.warning(
                    "While model state after last call to `gen` was recorded on the "
                    "las generator run produced by this generation strategy, it could"
                    " not be applied because model for this generation step is defined"
                    f" via factory function: {self._curr.model}. Generation strategies"
                    " with factory functions do not support reloading from a stored "
                    "state."
                )

        if not data.df.empty:
            trial_indices_in_data = sorted(data.df["trial_index"].unique())
            logger.debug(f"Fitting model with data for trials: {trial_indices_in_data}")

        self._curr.fit(experiment=self.experiment, data=data, **model_state_on_lgr)
        self._model = self._curr.model_spec.fitted_model

    def _update_current_model(self, new_data: Data) -> None:
        """Update the current model with new data (data for trials that have been
        completed since the last call to `GenerationStrategy.gen`).
        """
        if self._model is None:  # Should not be reachable.
            raise ValueError("Cannot update if no model instantiated.")
        trial_indices_in_new_data = sorted(new_data.df["trial_index"].unique())
        logger.info(f"Updating model with data for trials: {trial_indices_in_new_data}")
        # TODO[drfreund]: Switch to `self._curr.update` once `GenerationNode` supports
        not_none(self._model).update(experiment=self.experiment, new_data=new_data)

    def _get_data_for_update(self, passed_in_data: Optional[Data]) -> Optional[Data]:
        # Should only pass data that is new since last call to `gen`, to the
        # underlying model's `update`.
        newly_completed_trials = self._find_trials_completed_since_last_gen()
        if len(newly_completed_trials) == 0:
            logger.debug(
                "There were no newly completed trials since last model update."
            )
            return None

        if passed_in_data is None:
            new_data = self.experiment.lookup_data(trial_indices=newly_completed_trials)
            if new_data.df.empty:
                logger.info(
                    "No new data is attached to experiment; no need for model update."
                )
                return None
            return new_data

        elif passed_in_data.df.empty:
            logger.info("Manually supplied data is empty; no need for model update.")
            return None

        return Data(
            # pyre-ignore[6]: Expected `Optional[pd.core.frame.DataFrame]`
            # for 1st param. `df` to `Data.__init__` but got `pd.core.series.Series`
            df=passed_in_data.df[
                passed_in_data.df.trial_index.isin(newly_completed_trials)
            ]
        )

    # ------------------------- State-tracking helpers. -------------------------

    def _save_seen_trial_indices(self) -> None:
        """Saves Experiment's `trial_indices_by_status` at the time of the model's
        last `gen` (so these `trial_indices_by_status` reflect which trials model
        has seen the data for). Useful when `use_update=True` for a given
        generation step.
        """
        self._seen_trial_indices_by_status = deepcopy(
            self.experiment.trial_indices_by_status
        )

    def _find_trials_completed_since_last_gen(self) -> Set[int]:
        """Retrieves indices of trials that have been completed or updated with data
        since the last call to `GenerationStrategy.gen`.
        """
        completed_now = self.experiment.trial_indices_by_status[TrialStatus.COMPLETED]
        if self._seen_trial_indices_by_status is None:
            return completed_now

        completed_before = not_none(self._seen_trial_indices_by_status)[
            TrialStatus.COMPLETED
        ]
        return completed_now.difference(completed_before)

    def _register_trial_data_update(self, trial: BaseTrial) -> None:
        """Registers that a given trial has new data even though it's a trial that has
        been completed before. Useful only for generation steps that have `use_update=
        True`, as the information registered by this function is used for identifying
        new data since last call to `GenerationStrategy.gen`.
        """
        # TODO[T65857344]: store information about trial update to pass with `new_data`
        # to `model_update`. This information does not need to be stored, since when
        # restoring generation strategy from serialized form, all data will is
        # refetched and the underlying model is re-fit.
        if any(s.use_update for s in self._steps):
            raise NotImplementedError(
                "Updating completed trials with new data is not yet supported for "
                "generation strategies that leverage `model.update` functionality."
            )
