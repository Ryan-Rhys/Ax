#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import time
from logging import INFO
from typing import List, Optional, Tuple, Type

from ax.core.base_trial import BaseTrial
from ax.core.experiment import Experiment
from ax.core.generator_run import GeneratorRun
from ax.exceptions.core import UnsupportedError
from ax.modelbridge.generation_strategy import GenerationStrategy
from ax.utils.common.executils import retry_on_exception
from ax.utils.common.logger import _round_floats_for_logging, get_logger
from ax.utils.common.typeutils import not_none


RETRY_EXCEPTION_TYPES: Tuple[Type[Exception], ...] = ()
try:  # We don't require SQLAlchemy by default.
    from ax.storage.sqa_store.db import init_engine_and_session_factory
    from ax.storage.sqa_store.load import (
        _get_experiment_id,
        _get_generation_strategy_id,
        _load_experiment,
        _load_generation_strategy_by_experiment_name,
    )
    from ax.storage.sqa_store.save import (
        _save_experiment,
        _save_generation_strategy,
        _save_new_trials,
        _update_generation_strategy,
        _update_trials,
    )
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm.exc import StaleDataError
    from ax.storage.sqa_store.structs import DBSettings

    # We retry on `OperationalError` if saving to DB.
    RETRY_EXCEPTION_TYPES = (OperationalError, StaleDataError)
except ModuleNotFoundError:  # pragma: no cover
    DBSettings = None


logger = get_logger(__name__)


class WithDBSettingsBase:
    """Helper class providing methods for saving changes made to an experiment
    if `db_settings` property is set to a non-None value on the instance.
    """

    _db_settings: Optional[DBSettings] = None

    def __init__(
        self, db_settings: Optional[DBSettings] = None, logging_level: int = INFO
    ) -> None:
        if db_settings and (not DBSettings or not isinstance(db_settings, DBSettings)):
            raise ValueError(
                "`db_settings` argument should be of type ax.storage.sqa_store."
                "structs.DBSettings. To use `DBSettings`, you will need SQLAlchemy "
                "installed in your environment (can be installed through pip)."
            )
        self._db_settings = db_settings
        if self.db_settings_set:
            init_engine_and_session_factory(
                creator=self.db_settings.creator, url=self.db_settings.url
            )
        logger.setLevel(logging_level)

    @property
    def db_settings_set(self) -> bool:
        """Whether non-None DB settings are set on this instance."""
        return self._db_settings is not None

    @property
    def db_settings(self) -> DBSettings:
        """DB settings set on this instance; guaranteed to be non-None."""
        if self._db_settings is None:
            raise ValueError("No DB settings are set on this instance.")
        return not_none(self._db_settings)

    def _get_experiment_and_generation_strategy_db_id(
        self, experiment_name: str
    ) -> Tuple[Optional[int], Optional[int]]:
        """Retrieve DB ids of experiment by the given name and the associated
        generation strategy. Each ID is None if corresponding object is not
        found.
        """
        if not self.db_settings_set:
            return None, None

        exp_id = _get_experiment_id(
            experiment_name=experiment_name, decoder=self.db_settings.decoder
        )
        if not exp_id:
            return None, None
        gs_id = _get_generation_strategy_id(
            experiment_name=experiment_name, decoder=self.db_settings.decoder
        )
        return exp_id, gs_id

    def _maybe_save_experiment_and_generation_strategy(
        self, experiment: Experiment, generation_strategy: GenerationStrategy
    ) -> Tuple[bool, bool]:
        """If DB settings are set on this `WithDBSettingsBase` instance, checks
        whether given experiment and generation strategy are already saved and
        saves them, if not.

        Returns:
            Tuple of two booleans: whether experiment was saved in the course of
                this function's execution and whether generation strategy was
                saved.
        """
        saved_exp, saved_gs = False, False
        if self.db_settings_set:
            if experiment._name is None:
                raise ValueError(
                    "Experiment must specify a name to use storage functionality."
                )
            exp_name = not_none(experiment.name)
            exp_id, gs_id = self._get_experiment_and_generation_strategy_db_id(
                experiment_name=exp_name
            )
            if exp_id:  # Experiment in DB.
                # TODO: Switch to just updating experiment when selective-field
                # update is available.
                logger.info(f"Experiment {exp_name} is in DB, updating it.")
                self._save_experiment_to_db_if_possible(experiment=experiment)
                saved_exp = True
            else:  # Experiment not yet in DB.
                logger.info(f"Experiment {exp_name} is not yet in DB, storing it.")
                self._save_experiment_to_db_if_possible(experiment=experiment)
                saved_exp = True
            if gs_id and generation_strategy._db_id != gs_id:
                raise UnsupportedError(
                    "Experiment was associated with generation strategy in DB, "
                    f"but a new generation strategy {generation_strategy.name} "
                    "was provided. To use the generation strategy currently in DB,"
                    " instantiate scheduler via: `Scheduler.with_stored_experiment`."
                )
            if not gs_id or generation_strategy._db_id is None:
                # There is no GS associated with experiment or the generation
                # strategy passed in is different from the one associated with
                # experiment currently.
                logger.info(
                    f"Generation strategy {generation_strategy.name} is not yet in DB, "
                    "storing it."
                )
                # If generation strategy does not yet have an experiment attached,
                # attach the current experiment to it, as otherwise it will not be
                # possible to retrieve by experiment name.
                if generation_strategy._experiment is None:
                    generation_strategy.experiment = experiment
                self._save_generation_strategy_to_db_if_possible(
                    generation_strategy=generation_strategy
                )
                saved_gs = True
        return saved_exp, saved_gs

    def _load_experiment_and_generation_strategy(
        self, experiment_name: str
    ) -> Tuple[Optional[Experiment], Optional[GenerationStrategy]]:
        """Loads experiment and its corresponding generation strategy from database
        if DB settings are set on this `WithDBSettingsBase` instance.

        Args:
            experiment_name: Name of the experiment to load, used as unique
                identifier by which to find the experiment.

        Returns:
            - Tuple of `None` and `None` if `DBSettings` are set and no experiment
              exists by the given name.
            - Tuple of `Experiment` and `None` if experiment exists but does not
              have a generation strategy attached to it.
            - Tuple of `Experiment` and `GenerationStrategy` if experiment exists
              and has a generation strategy attached to it.
        """
        if not self.db_settings_set:
            raise ValueError("Cannot load from DB in absence of DB settings.")

        logger.info("Loading experiment and generation strategy...")
        start_time = time.time()
        experiment = _load_experiment(experiment_name, decoder=self.db_settings.decoder)
        if not isinstance(experiment, Experiment) or experiment.is_simple_experiment:
            raise ValueError("Service API only supports `Experiment`.")
        logger.info(
            f"Loaded experiment {experiment_name} in "
            f"{_round_floats_for_logging(time.time() - start_time)} seconds."
        )

        try:
            start_time = time.time()
            generation_strategy = _load_generation_strategy_by_experiment_name(
                experiment_name=experiment_name, decoder=self.db_settings.decoder
            )
            logger.info(
                f"Loaded generation strategy for experiment {experiment_name} in "
                f"{_round_floats_for_logging(time.time() - start_time)} seconds."
            )
        except ValueError as err:
            if "does not have a generation strategy" in str(err):
                return experiment, None
            raise  # `ValueError` here could signify more than just absence of GS.
        return experiment, generation_strategy

    @retry_on_exception(
        retries=3,
        default_return_on_suppression=False,
        exception_types=RETRY_EXCEPTION_TYPES,
    )
    def _save_experiment_to_db_if_possible(
        self, experiment: Experiment, suppress_all_errors: bool = False
    ) -> bool:
        """Saves attached experiment and generation strategy if DB settings are
        set on this `WithDBSettingsBase` instance.

        Args:
            experiment: Experiment to save new trials in DB.
            suppress_all_errors: Flag for `retry_on_exception` that makes
                the decorator suppress the thrown exception even if it
                occurred in all the retries (exception is still logged).

        Returns:
            bool: Whether the experiment was saved.
        """
        if self.db_settings_set:
            start_time = time.time()
            _save_experiment(experiment, encoder=self.db_settings.encoder)
            logger.debug(
                f"Saved experiment {experiment.name} in "
                f"{_round_floats_for_logging(time.time() - start_time)} seconds."
            )
            return True
        return False

    @retry_on_exception(
        retries=3,
        default_return_on_suppression=False,
        exception_types=RETRY_EXCEPTION_TYPES,
    )
    def _save_new_trial_to_db_if_possible(
        self,
        experiment: Experiment,
        trial: BaseTrial,
        suppress_all_errors: bool = False,
    ) -> bool:
        """Saves new trial on given experiment if DB settings are set on this
        `WithDBSettingsBase` instance.

        Args:
            experiment: Experiment, on which to save new trial in DB.
            trials: Newly added trial to save.
            suppress_all_errors: Flag for `retry_on_exception` that makes
                the decorator suppress the thrown exception even if it
                occurred in all the retries (exception is still logged).

        Returns:
            bool: Whether the trial was saved.
        """
        return self._save_new_trials_to_db_if_possible(
            experiment, [trial], suppress_all_errors
        )

    @retry_on_exception(
        retries=3,
        default_return_on_suppression=False,
        exception_types=RETRY_EXCEPTION_TYPES,
    )
    def _save_new_trials_to_db_if_possible(
        self,
        experiment: Experiment,
        trials: List[BaseTrial],
        suppress_all_errors: bool = False,
    ) -> bool:
        """Saves new trials on given experiment if DB settings are set on this
        `WithDBSettingsBase` instance.

        Args:
            experiment: Experiment, on which to save new trials in DB.
            trials: Newly added trials to save.
            suppress_all_errors: Flag for `retry_on_exception` that makes
                the decorator suppress the thrown exception even if it
                occurred in all the retries (exception is still logged).

        Returns:
            bool: Whether the trials were saved.
        """
        if self.db_settings_set:
            start_time = time.time()
            _save_new_trials(
                experiment=experiment, trials=trials, encoder=self.db_settings.encoder
            )
            logger.debug(
                f"Saved trials {[trial.index for trial in trials]} in "
                f"{_round_floats_for_logging(time.time() - start_time)} seconds."
            )
            return True
        return False

    @retry_on_exception(
        retries=3,
        default_return_on_suppression=False,
        exception_types=RETRY_EXCEPTION_TYPES,
    )
    def _save_updated_trial_to_db_if_possible(
        self,
        experiment: Experiment,
        trial: BaseTrial,
        suppress_all_errors: bool = False,
    ) -> bool:
        """Saves updated trials on given experiment if DB settings are set on this
        `WithDBSettingsBase` instance.

        Args:
            experiment: Experiment, on which to save updated trials in DB.
            trial: Newly updated trial to save.
            suppress_all_errors: Flag for `retry_on_exception` that makes
                the decorator suppress the thrown exception even if it
                occurred in all the retries (exception is still logged).

        Returns:
            bool: Whether the trial was saved.
        """
        return self._save_updated_trials_to_db_if_possible(
            experiment, [trial], suppress_all_errors
        )

    @retry_on_exception(
        retries=3,
        default_return_on_suppression=False,
        exception_types=RETRY_EXCEPTION_TYPES,
    )
    def _save_updated_trials_to_db_if_possible(
        self,
        experiment: Experiment,
        trials: List[BaseTrial],
        suppress_all_errors: bool = False,
    ) -> bool:
        """Saves updated trials on given experiment if DB settings are set on this
        `WithDBSettingsBase` instance.

        Args:
            experiment: Experiment, on which to save updated trials in DB.
            trials: Newly updated trials to save.
            suppress_all_errors: Flag for `retry_on_exception` that makes
                the decorator suppress the thrown exception even if it
                occurred in all the retries (exception is still logged).

        Returns:
            bool: Whether the trials were saved.
        """
        if self.db_settings_set:
            start_time = time.time()
            _update_trials(
                experiment=experiment, trials=trials, encoder=self.db_settings.encoder
            )
            logger.debug(
                f"Updated trials {[trial.index for trial in trials]} in "
                f"{_round_floats_for_logging(time.time() - start_time)} seconds."
            )
            return True
        return False

    @retry_on_exception(
        retries=3,
        default_return_on_suppression=False,
        exception_types=RETRY_EXCEPTION_TYPES,
    )
    def _save_generation_strategy_to_db_if_possible(
        self, generation_strategy: GenerationStrategy, suppress_all_errors: bool = False
    ) -> bool:
        """Saves given generation strategy if DB settings are set on this
        `WithDBSettingsBase` instance.

        Args:
            generation_strategy: Generation strategy to save in DB.
            suppress_all_errors: Flag for `retry_on_exception` that makes
                the decorator suppress the thrown exception even if it
                occurred in all the retries (exception is still logged).

        Returns:
            bool: Whether the generation strategy was saved.
        """
        if self.db_settings_set:
            start_time = time.time()
            _save_generation_strategy(
                generation_strategy=generation_strategy,
                encoder=self.db_settings.encoder,
            )
            logger.debug(
                f"Saved generation strategy {generation_strategy.name} in "
                f"{_round_floats_for_logging(time.time() - start_time)} seconds."
            )
            return True
        return False

    @retry_on_exception(
        retries=3,
        default_return_on_suppression=False,
        exception_types=RETRY_EXCEPTION_TYPES,
    )
    def _update_generation_strategy_in_db_if_possible(
        self,
        generation_strategy: GenerationStrategy,
        new_generator_runs: List[GeneratorRun],
        suppress_all_errors: bool = False,
    ) -> bool:
        """Updates the given generation strategy with new generator runs (and with
        new current generation step if applicable) if DB settings are set
        on this `WithDBSettingsBase` instance.

        Args:
            generation_strategy: Generation strategy to update in DB.
            new_generator_runs: New generator runs of this generation strategy
                since its last save.
            suppress_all_errors: Flag for `retry_on_exception` that makes
                the decorator suppress the thrown exception even if it
                occurred in all the retries (exception is still logged).
        Returns:
            bool: Whether the experiment was saved.
        """
        if self.db_settings_set:
            start_time = time.time()
            _update_generation_strategy(
                generation_strategy=generation_strategy,
                generator_runs=new_generator_runs,
                encoder=self.db_settings.encoder,
            )
            logger.debug(
                f"Updated generation strategy {generation_strategy.name} in "
                f"{_round_floats_for_logging(time.time() - start_time)} seconds."
            )
            return True
        return False
