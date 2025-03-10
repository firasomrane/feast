# Copyright 2019 The Feast Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import copy
import itertools
import os
import warnings
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
    cast,
)

import pandas as pd
import pyarrow as pa
from colorama import Fore, Style
from google.protobuf.timestamp_pb2 import Timestamp
from tqdm import tqdm

from feast import feature_server, ui_server, utils
from feast.base_feature_view import BaseFeatureView
from feast.batch_feature_view import BatchFeatureView
from feast.data_source import DataSource, PushMode
from feast.diff.infra_diff import InfraDiff, diff_infra_protos
from feast.diff.registry_diff import RegistryDiff, apply_diff_to_registry, diff_between
from feast.dqm.errors import ValidationFailed
from feast.entity import Entity
from feast.errors import (
    EntityNotFoundException,
    FeatureNameCollisionError,
    FeatureViewNotFoundException,
    RequestDataNotFoundInEntityDfException,
    RequestDataNotFoundInEntityRowsException,
)
from feast.feast_object import FeastObject
from feast.feature_service import FeatureService
from feast.feature_view import (
    DUMMY_ENTITY,
    DUMMY_ENTITY_ID,
    DUMMY_ENTITY_NAME,
    DUMMY_ENTITY_VAL,
    FeatureView,
)
from feast.inference import (
    update_data_sources_with_inferred_event_timestamp_col,
    update_feature_views_with_inferred_features_and_entities,
)
from feast.infra.infra_object import Infra
from feast.infra.provider import Provider, RetrievalJob, get_provider
from feast.infra.registry_stores.sql import SqlRegistry
from feast.on_demand_feature_view import OnDemandFeatureView
from feast.online_response import OnlineResponse
from feast.protos.feast.serving.ServingService_pb2 import (
    FieldStatus,
    GetOnlineFeaturesResponse,
)
from feast.protos.feast.types.EntityKey_pb2 import EntityKey as EntityKeyProto
from feast.protos.feast.types.Value_pb2 import RepeatedValue, Value
from feast.registry import BaseRegistry, Registry
from feast.repo_config import RepoConfig, load_repo_config
from feast.repo_contents import RepoContents
from feast.request_feature_view import RequestFeatureView
from feast.saved_dataset import SavedDataset, SavedDatasetStorage, ValidationReference
from feast.stream_feature_view import StreamFeatureView
from feast.type_map import (
    feast_value_type_to_python_type,
    python_values_to_proto_values,
)
from feast.usage import log_exceptions, log_exceptions_and_usage, set_usage_attribute
from feast.value_type import ValueType
from feast.version import get_version

warnings.simplefilter("once", DeprecationWarning)

if TYPE_CHECKING:
    from feast.embedded_go.online_features_service import EmbeddedOnlineFeatureServer


class FeatureStore:
    """
    A FeatureStore object is used to define, create, and retrieve features.

    Args:
        repo_path (optional): Path to a `feature_store.yaml` used to configure the
            feature store.
        config (optional): Configuration object used to configure the feature store.
    """

    config: RepoConfig
    repo_path: Path
    _registry: BaseRegistry
    _provider: Provider
    _go_server: "EmbeddedOnlineFeatureServer"

    @log_exceptions
    def __init__(
        self,
        repo_path: Optional[str] = None,
        config: Optional[RepoConfig] = None,
    ):
        """
        Creates a FeatureStore object.

        Raises:
            ValueError: If both or neither of repo_path and config are specified.
        """
        if repo_path is not None and config is not None:
            raise ValueError("You cannot specify both repo_path and config.")
        if config is not None:
            self.repo_path = Path(os.getcwd())
            self.config = config
        elif repo_path is not None:
            self.repo_path = Path(repo_path)
            self.config = load_repo_config(Path(repo_path))
        else:
            raise ValueError("Please specify one of repo_path or config.")

        registry_config = self.config.get_registry_config()
        if registry_config.registry_type == "sql":
            self._registry = SqlRegistry(registry_config, None)
        else:
            r = Registry(registry_config, repo_path=self.repo_path)
            r._initialize_registry(self.config.project)
            self._registry = r
        self._provider = get_provider(self.config, self.repo_path)
        self._go_server = None

    @log_exceptions
    def version(self) -> str:
        """Returns the version of the current Feast SDK/CLI."""
        return get_version()

    @property
    def registry(self) -> BaseRegistry:
        """Gets the registry of this feature store."""
        return self._registry

    @property
    def project(self) -> str:
        """Gets the project of this feature store."""
        return self.config.project

    def _get_provider(self) -> Provider:
        # TODO: Bake self.repo_path into self.config so that we dont only have one interface to paths
        return self._provider

    @log_exceptions_and_usage
    def refresh_registry(self):
        """Fetches and caches a copy of the feature registry in memory.

        Explicitly calling this method allows for direct control of the state of the registry cache. Every time this
        method is called the complete registry state will be retrieved from the remote registry store backend
        (e.g., GCS, S3), and the cache timer will be reset. If refresh_registry() is run before get_online_features()
        is called, then get_online_features() will use the cached registry instead of retrieving (and caching) the
        registry itself.

        Additionally, the TTL for the registry cache can be set to infinity (by setting it to 0), which means that
        refresh_registry() will become the only way to update the cached registry. If the TTL is set to a value
        greater than 0, then once the cache becomes stale (more time than the TTL has passed), a new cache will be
        downloaded synchronously, which may increase latencies if the triggering method is get_online_features().
        """
        registry_config = self.config.get_registry_config()
        registry = Registry(registry_config, repo_path=self.repo_path)
        registry.refresh(self.config.project)

        self._registry = registry

    @log_exceptions_and_usage
    def list_entities(self, allow_cache: bool = False) -> List[Entity]:
        """
        Retrieves the list of entities from the registry.

        Args:
            allow_cache: Whether to allow returning entities from a cached registry.

        Returns:
            A list of entities.
        """
        return self._list_entities(allow_cache)

    def _list_entities(
        self, allow_cache: bool = False, hide_dummy_entity: bool = True
    ) -> List[Entity]:
        all_entities = self._registry.list_entities(
            self.project, allow_cache=allow_cache
        )
        return [
            entity
            for entity in all_entities
            if entity.name != DUMMY_ENTITY_NAME or not hide_dummy_entity
        ]

    @log_exceptions_and_usage
    def list_feature_services(self) -> List[FeatureService]:
        """
        Retrieves the list of feature services from the registry.

        Returns:
            A list of feature services.
        """
        return self._registry.list_feature_services(self.project)

    @log_exceptions_and_usage
    def list_feature_views(self, allow_cache: bool = False) -> List[FeatureView]:
        """
        Retrieves the list of feature views from the registry.

        Args:
            allow_cache: Whether to allow returning entities from a cached registry.

        Returns:
            A list of feature views.
        """
        return self._list_feature_views(allow_cache)

    @log_exceptions_and_usage
    def list_request_feature_views(
        self, allow_cache: bool = False
    ) -> List[RequestFeatureView]:
        """
        Retrieves the list of feature views from the registry.

        Args:
            allow_cache: Whether to allow returning entities from a cached registry.

        Returns:
            A list of feature views.
        """
        return self._registry.list_request_feature_views(
            self.project, allow_cache=allow_cache
        )

    def _list_feature_views(
        self,
        allow_cache: bool = False,
        hide_dummy_entity: bool = True,
    ) -> List[FeatureView]:
        feature_views = []
        for fv in self._registry.list_feature_views(
            self.project, allow_cache=allow_cache
        ):
            if hide_dummy_entity and fv.entities[0] == DUMMY_ENTITY_NAME:
                fv.entities = []
                fv.entity_columns = []
            feature_views.append(fv)
        return feature_views

    def _list_stream_feature_views(
        self,
        allow_cache: bool = False,
        hide_dummy_entity: bool = True,
    ) -> List[StreamFeatureView]:
        stream_feature_views = []
        for sfv in self._registry.list_stream_feature_views(
            self.project, allow_cache=allow_cache
        ):
            if hide_dummy_entity and sfv.entities[0] == DUMMY_ENTITY_NAME:
                sfv.entities = []
                sfv.entity_columns = []
            stream_feature_views.append(sfv)
        return stream_feature_views

    @log_exceptions_and_usage
    def list_on_demand_feature_views(
        self, allow_cache: bool = False
    ) -> List[OnDemandFeatureView]:
        """
        Retrieves the list of on demand feature views from the registry.

        Returns:
            A list of on demand feature views.
        """
        return self._registry.list_on_demand_feature_views(
            self.project, allow_cache=allow_cache
        )

    @log_exceptions_and_usage
    def list_stream_feature_views(
        self, allow_cache: bool = False
    ) -> List[StreamFeatureView]:
        """
        Retrieves the list of stream feature views from the registry.

        Returns:
            A list of stream feature views.
        """
        return self._list_stream_feature_views(allow_cache)

    @log_exceptions_and_usage
    def list_data_sources(self, allow_cache: bool = False) -> List[DataSource]:
        """
        Retrieves the list of data sources from the registry.

        Args:
            allow_cache: Whether to allow returning data sources from a cached registry.

        Returns:
            A list of data sources.
        """
        return self._registry.list_data_sources(self.project, allow_cache=allow_cache)

    @log_exceptions_and_usage
    def get_entity(self, name: str, allow_registry_cache: bool = False) -> Entity:
        """
        Retrieves an entity.

        Args:
            name: Name of entity.
            allow_registry_cache: (Optional) Whether to allow returning this entity from a cached registry

        Returns:
            The specified entity.

        Raises:
            EntityNotFoundException: The entity could not be found.
        """
        return self._registry.get_entity(
            name, self.project, allow_cache=allow_registry_cache
        )

    @log_exceptions_and_usage
    def get_feature_service(
        self, name: str, allow_cache: bool = False
    ) -> FeatureService:
        """
        Retrieves a feature service.

        Args:
            name: Name of feature service.
            allow_cache: Whether to allow returning feature services from a cached registry.

        Returns:
            The specified feature service.

        Raises:
            FeatureServiceNotFoundException: The feature service could not be found.
        """
        return self._registry.get_feature_service(name, self.project, allow_cache)

    @log_exceptions_and_usage
    def get_feature_view(
        self, name: str, allow_registry_cache: bool = False
    ) -> FeatureView:
        """
        Retrieves a feature view.

        Args:
            name: Name of feature view.
            allow_registry_cache: (Optional) Whether to allow returning this entity from a cached registry

        Returns:
            The specified feature view.

        Raises:
            FeatureViewNotFoundException: The feature view could not be found.
        """
        return self._get_feature_view(name, allow_registry_cache=allow_registry_cache)

    def _get_feature_view(
        self,
        name: str,
        hide_dummy_entity: bool = True,
        allow_registry_cache: bool = False,
    ) -> FeatureView:
        feature_view = self._registry.get_feature_view(
            name, self.project, allow_cache=allow_registry_cache
        )
        if hide_dummy_entity and feature_view.entities[0] == DUMMY_ENTITY_NAME:
            feature_view.entities = []
        return feature_view

    @log_exceptions_and_usage
    def get_stream_feature_view(
        self, name: str, allow_registry_cache: bool = False
    ) -> StreamFeatureView:
        """
        Retrieves a stream feature view.

        Args:
            name: Name of stream feature view.
            allow_registry_cache: (Optional) Whether to allow returning this entity from a cached registry

        Returns:
            The specified stream feature view.

        Raises:
            FeatureViewNotFoundException: The feature view could not be found.
        """
        return self._get_stream_feature_view(
            name, allow_registry_cache=allow_registry_cache
        )

    def _get_stream_feature_view(
        self,
        name: str,
        hide_dummy_entity: bool = True,
        allow_registry_cache: bool = False,
    ) -> StreamFeatureView:
        stream_feature_view = self._registry.get_stream_feature_view(
            name, self.project, allow_cache=allow_registry_cache
        )
        if hide_dummy_entity and stream_feature_view.entities[0] == DUMMY_ENTITY_NAME:
            stream_feature_view.entities = []
        return stream_feature_view

    @log_exceptions_and_usage
    def get_on_demand_feature_view(self, name: str) -> OnDemandFeatureView:
        """
        Retrieves a feature view.

        Args:
            name: Name of feature view.

        Returns:
            The specified feature view.

        Raises:
            FeatureViewNotFoundException: The feature view could not be found.
        """
        return self._registry.get_on_demand_feature_view(name, self.project)

    @log_exceptions_and_usage
    def get_data_source(self, name: str) -> DataSource:
        """
        Retrieves the list of data sources from the registry.

        Args:
            name: Name of the data source.

        Returns:
            The specified data source.

        Raises:
            DataSourceObjectNotFoundException: The data source could not be found.
        """
        return self._registry.get_data_source(name, self.project)

    @log_exceptions_and_usage
    def delete_feature_view(self, name: str):
        """
        Deletes a feature view.

        Args:
            name: Name of feature view.

        Raises:
            FeatureViewNotFoundException: The feature view could not be found.
        """
        return self._registry.delete_feature_view(name, self.project)

    @log_exceptions_and_usage
    def delete_feature_service(self, name: str):
        """
        Deletes a feature service.

        Args:
            name: Name of feature service.

        Raises:
            FeatureServiceNotFoundException: The feature view could not be found.
        """
        return self._registry.delete_feature_service(name, self.project)

    def _get_features(
        self,
        features: Union[List[str], FeatureService],
        allow_cache: bool = False,
    ) -> List[str]:
        _features = features

        if not _features:
            raise ValueError("No features specified for retrieval")

        _feature_refs = []
        if isinstance(_features, FeatureService):
            feature_service_from_registry = self.get_feature_service(
                _features.name, allow_cache
            )
            if feature_service_from_registry != _features:
                warnings.warn(
                    "The FeatureService object that has been passed in as an argument is "
                    "inconsistent with the version from the registry. Potentially a newer version "
                    "of the FeatureService has been applied to the registry."
                )
            for projection in feature_service_from_registry.feature_view_projections:
                _feature_refs.extend(
                    [
                        f"{projection.name_to_use()}:{f.name}"
                        for f in projection.features
                    ]
                )
        else:
            assert isinstance(_features, list)
            _feature_refs = _features
        return _feature_refs

    def _should_use_plan(self):
        """Returns True if plan and _apply_diffs should be used, False otherwise."""
        # Currently only the local provider with sqlite online store supports plan and _apply_diffs.
        return self.config.provider == "local" and (
            self.config.online_store and self.config.online_store.type == "sqlite"
        )

    def _validate_all_feature_views(
        self,
        views_to_update: List[FeatureView],
        odfvs_to_update: List[OnDemandFeatureView],
        request_views_to_update: List[RequestFeatureView],
        sfvs_to_update: List[StreamFeatureView],
    ):
        """Validates all feature views."""
        if len(odfvs_to_update) > 0:
            warnings.warn(
                "On demand feature view is an experimental feature. "
                "This API is stable, but the functionality does not scale well for offline retrieval",
                RuntimeWarning,
            )

        set_usage_attribute("odfv", bool(odfvs_to_update))

        _validate_feature_views(
            [
                *views_to_update,
                *odfvs_to_update,
                *request_views_to_update,
                *sfvs_to_update,
            ]
        )

    def _make_inferences(
        self,
        data_sources_to_update: List[DataSource],
        entities_to_update: List[Entity],
        views_to_update: List[FeatureView],
        odfvs_to_update: List[OnDemandFeatureView],
        sfvs_to_update: List[StreamFeatureView],
        feature_services_to_update: List[FeatureService],
    ):
        """Makes inferences for entities, feature views, odfvs, and feature services."""
        update_data_sources_with_inferred_event_timestamp_col(
            data_sources_to_update, self.config
        )

        update_data_sources_with_inferred_event_timestamp_col(
            [view.batch_source for view in views_to_update], self.config
        )

        update_data_sources_with_inferred_event_timestamp_col(
            [view.batch_source for view in sfvs_to_update], self.config
        )

        # New feature views may reference previously applied entities.
        entities = self._list_entities()
        update_feature_views_with_inferred_features_and_entities(
            views_to_update, entities + entities_to_update, self.config
        )
        update_feature_views_with_inferred_features_and_entities(
            sfvs_to_update, entities + entities_to_update, self.config
        )
        # TODO(kevjumba): Update schema inferrence
        for sfv in sfvs_to_update:
            if not sfv.schema:
                raise ValueError(
                    f"schema inference not yet supported for stream feature views. please define schema for stream feature view: {sfv.name}"
                )

        for odfv in odfvs_to_update:
            odfv.infer_features()

        fvs_to_update_map = {
            view.name: view for view in [*views_to_update, *sfvs_to_update]
        }
        for feature_service in feature_services_to_update:
            feature_service.infer_features(fvs_to_update=fvs_to_update_map)

    def _get_feature_views_to_materialize(
        self,
        feature_views: Optional[List[str]],
    ) -> List[FeatureView]:
        """
        Returns the list of feature views that should be materialized.

        If no feature views are specified, all feature views will be returned.

        Args:
            feature_views: List of names of feature views to materialize.

        Raises:
            FeatureViewNotFoundException: One of the specified feature views could not be found.
            ValueError: One of the specified feature views is not configured for materialization.
        """
        feature_views_to_materialize: List[FeatureView] = []

        if feature_views is None:
            feature_views_to_materialize = self._list_feature_views(
                hide_dummy_entity=False
            )
            feature_views_to_materialize = [
                fv for fv in feature_views_to_materialize if fv.online
            ]
            stream_feature_views_to_materialize = self._list_stream_feature_views(
                hide_dummy_entity=False
            )
            feature_views_to_materialize += [
                sfv for sfv in stream_feature_views_to_materialize if sfv.online
            ]
        else:
            for name in feature_views:
                try:
                    feature_view = self._get_feature_view(name, hide_dummy_entity=False)
                except FeatureViewNotFoundException:
                    feature_view = self._get_stream_feature_view(
                        name, hide_dummy_entity=False
                    )

                if not feature_view.online:
                    raise ValueError(
                        f"FeatureView {feature_view.name} is not configured to be served online."
                    )
                feature_views_to_materialize.append(feature_view)

        return feature_views_to_materialize

    @log_exceptions_and_usage
    def plan(
        self, desired_repo_contents: RepoContents
    ) -> Tuple[RegistryDiff, InfraDiff, Infra]:
        """Dry-run registering objects to metadata store.

        The plan method dry-runs registering one or more definitions (e.g., Entity, FeatureView), and produces
        a list of all the changes the that would be introduced in the feature repo. The changes computed by the plan
        command are for informational purposes, and are not actually applied to the registry.

        Args:
            desired_repo_contents: The desired repo state.

        Raises:
            ValueError: The 'objects' parameter could not be parsed properly.

        Examples:
            Generate a plan adding an Entity and a FeatureView.

            >>> from feast import FeatureStore, Entity, FeatureView, Feature, FileSource, RepoConfig
            >>> from feast.feature_store import RepoContents
            >>> from datetime import timedelta
            >>> fs = FeatureStore(repo_path="feature_repo")
            >>> driver = Entity(name="driver_id", description="driver id")
            >>> driver_hourly_stats = FileSource(
            ...     path="feature_repo/data/driver_stats.parquet",
            ...     timestamp_field="event_timestamp",
            ...     created_timestamp_column="created",
            ... )
            >>> driver_hourly_stats_view = FeatureView(
            ...     name="driver_hourly_stats",
            ...     entities=[driver],
            ...     ttl=timedelta(seconds=86400 * 1),
            ...     batch_source=driver_hourly_stats,
            ... )
            >>> registry_diff, infra_diff, new_infra = fs.plan(RepoContents(
            ...     data_sources=[driver_hourly_stats],
            ...     feature_views=[driver_hourly_stats_view],
            ...     on_demand_feature_views=list(),
            ...     stream_feature_views=list(),
            ...     request_feature_views=list(),
            ...     entities=[driver],
            ...     feature_services=list())) # register entity and feature view
        """
        # Validate and run inference on all the objects to be registered.
        self._validate_all_feature_views(
            desired_repo_contents.feature_views,
            desired_repo_contents.on_demand_feature_views,
            desired_repo_contents.request_feature_views,
            desired_repo_contents.stream_feature_views,
        )
        _validate_data_sources(desired_repo_contents.data_sources)
        self._make_inferences(
            desired_repo_contents.data_sources,
            desired_repo_contents.entities,
            desired_repo_contents.feature_views,
            desired_repo_contents.on_demand_feature_views,
            desired_repo_contents.stream_feature_views,
            desired_repo_contents.feature_services,
        )

        # Compute the desired difference between the current objects in the registry and
        # the desired repo state.
        registry_diff = diff_between(
            self._registry, self.project, desired_repo_contents
        )

        # Compute the desired difference between the current infra, as stored in the registry,
        # and the desired infra.
        self._registry.refresh(self.project)
        current_infra_proto = self._registry.proto().infra.__deepcopy__()
        desired_registry_proto = desired_repo_contents.to_registry_proto()
        new_infra = self._provider.plan_infra(self.config, desired_registry_proto)
        new_infra_proto = new_infra.to_proto()
        infra_diff = diff_infra_protos(current_infra_proto, new_infra_proto)

        return registry_diff, infra_diff, new_infra

    @log_exceptions_and_usage
    def _apply_diffs(
        self, registry_diff: RegistryDiff, infra_diff: InfraDiff, new_infra: Infra
    ):
        """Applies the given diffs to the metadata store and infrastructure.

        Args:
            registry_diff: The diff between the current registry and the desired registry.
            infra_diff: The diff between the current infra and the desired infra.
            new_infra: The desired infra.
        """
        infra_diff.update()
        apply_diff_to_registry(
            self._registry, registry_diff, self.project, commit=False
        )

        self._registry.update_infra(new_infra, self.project, commit=True)

    @log_exceptions_and_usage
    def apply(
        self,
        objects: Union[
            DataSource,
            Entity,
            FeatureView,
            OnDemandFeatureView,
            RequestFeatureView,
            StreamFeatureView,
            FeatureService,
            ValidationReference,
            List[FeastObject],
        ],
        objects_to_delete: Optional[List[FeastObject]] = None,
        partial: bool = True,
    ):
        """Register objects to metadata store and update related infrastructure.

        The apply method registers one or more definitions (e.g., Entity, FeatureView) and registers or updates these
        objects in the Feast registry. Once the apply method has updated the infrastructure (e.g., create tables in
        an online store), it will commit the updated registry. All operations are idempotent, meaning they can safely
        be rerun.

        Args:
            objects: A single object, or a list of objects that should be registered with the Feature Store.
            objects_to_delete: A list of objects to be deleted from the registry and removed from the
                provider's infrastructure. This deletion will only be performed if partial is set to False.
            partial: If True, apply will only handle the specified objects; if False, apply will also delete
                all the objects in objects_to_delete, and tear down any associated cloud resources.

        Raises:
            ValueError: The 'objects' parameter could not be parsed properly.

        Examples:
            Register an Entity and a FeatureView.

            >>> from feast import FeatureStore, Entity, FeatureView, Feature, FileSource, RepoConfig
            >>> from datetime import timedelta
            >>> fs = FeatureStore(repo_path="feature_repo")
            >>> driver = Entity(name="driver_id", description="driver id")
            >>> driver_hourly_stats = FileSource(
            ...     path="feature_repo/data/driver_stats.parquet",
            ...     timestamp_field="event_timestamp",
            ...     created_timestamp_column="created",
            ... )
            >>> driver_hourly_stats_view = FeatureView(
            ...     name="driver_hourly_stats",
            ...     entities=[driver],
            ...     ttl=timedelta(seconds=86400 * 1),
            ...     batch_source=driver_hourly_stats,
            ... )
            >>> fs.apply([driver_hourly_stats_view, driver]) # register entity and feature view
        """
        # TODO: Add locking
        if not isinstance(objects, Iterable):
            objects = [objects]
        assert isinstance(objects, list)

        if not objects_to_delete:
            objects_to_delete = []

        # Separate all objects into entities, feature services, and different feature view types.
        entities_to_update = [ob for ob in objects if isinstance(ob, Entity)]
        views_to_update = [
            ob
            for ob in objects
            if (
                isinstance(ob, FeatureView)
                and not isinstance(ob, StreamFeatureView)
                and not isinstance(ob, BatchFeatureView)
            )
        ]
        sfvs_to_update = [ob for ob in objects if isinstance(ob, StreamFeatureView)]
        request_views_to_update = [
            ob for ob in objects if isinstance(ob, RequestFeatureView)
        ]
        odfvs_to_update = [ob for ob in objects if isinstance(ob, OnDemandFeatureView)]
        services_to_update = [ob for ob in objects if isinstance(ob, FeatureService)]
        data_sources_set_to_update = {
            ob for ob in objects if isinstance(ob, DataSource)
        }
        validation_references_to_update = [
            ob for ob in objects if isinstance(ob, ValidationReference)
        ]

        for fv in itertools.chain(views_to_update, sfvs_to_update):
            data_sources_set_to_update.add(fv.batch_source)
            if fv.stream_source:
                data_sources_set_to_update.add(fv.stream_source)

        if request_views_to_update:
            warnings.warn(
                "Request feature view is deprecated. "
                "Please use request data source instead",
                DeprecationWarning,
            )

        for rfv in request_views_to_update:
            data_sources_set_to_update.add(rfv.request_data_source)

        for odfv in odfvs_to_update:
            for v in odfv.source_request_sources.values():
                data_sources_set_to_update.add(v)

        data_sources_to_update = list(data_sources_set_to_update)

        # Handle all entityless feature views by using DUMMY_ENTITY as a placeholder entity.
        entities_to_update.append(DUMMY_ENTITY)

        # Validate all feature views and make inferences.
        self._validate_all_feature_views(
            views_to_update, odfvs_to_update, request_views_to_update, sfvs_to_update
        )
        self._make_inferences(
            data_sources_to_update,
            entities_to_update,
            views_to_update,
            odfvs_to_update,
            sfvs_to_update,
            services_to_update,
        )

        # Add all objects to the registry and update the provider's infrastructure.
        for ds in data_sources_to_update:
            self._registry.apply_data_source(ds, project=self.project, commit=False)
        for view in itertools.chain(
            views_to_update, odfvs_to_update, request_views_to_update, sfvs_to_update
        ):
            self._registry.apply_feature_view(view, project=self.project, commit=False)
        for ent in entities_to_update:
            self._registry.apply_entity(ent, project=self.project, commit=False)
        for feature_service in services_to_update:
            self._registry.apply_feature_service(
                feature_service, project=self.project, commit=False
            )
        for validation_references in validation_references_to_update:
            self._registry.apply_validation_reference(
                validation_references, project=self.project, commit=False
            )

        if not partial:
            # Delete all registry objects that should not exist.
            entities_to_delete = [
                ob for ob in objects_to_delete if isinstance(ob, Entity)
            ]
            views_to_delete = [
                ob for ob in objects_to_delete if isinstance(ob, FeatureView)
            ]
            request_views_to_delete = [
                ob for ob in objects_to_delete if isinstance(ob, RequestFeatureView)
            ]
            odfvs_to_delete = [
                ob for ob in objects_to_delete if isinstance(ob, OnDemandFeatureView)
            ]
            sfvs_to_delete = [
                ob for ob in objects_to_delete if isinstance(ob, StreamFeatureView)
            ]
            services_to_delete = [
                ob for ob in objects_to_delete if isinstance(ob, FeatureService)
            ]
            data_sources_to_delete = [
                ob for ob in objects_to_delete if isinstance(ob, DataSource)
            ]
            validation_references_to_delete = [
                ob for ob in objects_to_delete if isinstance(ob, ValidationReference)
            ]

            for data_source in data_sources_to_delete:
                self._registry.delete_data_source(
                    data_source.name, project=self.project, commit=False
                )
            for entity in entities_to_delete:
                self._registry.delete_entity(
                    entity.name, project=self.project, commit=False
                )
            for view in views_to_delete:
                self._registry.delete_feature_view(
                    view.name, project=self.project, commit=False
                )
            for request_view in request_views_to_delete:
                self._registry.delete_feature_view(
                    request_view.name, project=self.project, commit=False
                )
            for odfv in odfvs_to_delete:
                self._registry.delete_feature_view(
                    odfv.name, project=self.project, commit=False
                )
            for sfv in sfvs_to_delete:
                self._registry.delete_feature_view(
                    sfv.name, project=self.project, commit=False
                )
            for service in services_to_delete:
                self._registry.delete_feature_service(
                    service.name, project=self.project, commit=False
                )
            for validation_references in validation_references_to_delete:
                self._registry.delete_validation_reference(
                    validation_references.name, project=self.project, commit=False
                )

        self._get_provider().update_infra(
            project=self.project,
            tables_to_delete=views_to_delete + sfvs_to_delete if not partial else [],
            tables_to_keep=views_to_update + sfvs_to_update,
            entities_to_delete=entities_to_delete if not partial else [],
            entities_to_keep=entities_to_update,
            partial=partial,
        )

        self._registry.commit()

        # go server needs to be reloaded to apply new configuration.
        # we're stopping it here
        # new server will be instantiated on the next online request
        self._teardown_go_server()

    @log_exceptions_and_usage
    def teardown(self):
        """Tears down all local and cloud resources for the feature store."""
        tables: List[FeatureView] = []
        feature_views = self.list_feature_views()

        tables.extend(feature_views)

        entities = self.list_entities()

        self._get_provider().teardown_infra(self.project, tables, entities)
        self._registry.teardown()
        self._teardown_go_server()

    @log_exceptions_and_usage
    def get_historical_features(
        self,
        entity_df: Union[pd.DataFrame, str],
        features: Union[List[str], FeatureService],
        full_feature_names: bool = False,
    ) -> RetrievalJob:
        """Enrich an entity dataframe with historical feature values for either training or batch scoring.

        This method joins historical feature data from one or more feature views to an entity dataframe by using a time
        travel join.

        Each feature view is joined to the entity dataframe using all entities configured for the respective feature
        view. All configured entities must be available in the entity dataframe. Therefore, the entity dataframe must
        contain all entities found in all feature views, but the individual feature views can have different entities.

        Time travel is based on the configured TTL for each feature view. A shorter TTL will limit the
        amount of scanning that will be done in order to find feature data for a specific entity key. Setting a short
        TTL may result in null values being returned.

        Args:
            entity_df (Union[pd.DataFrame, str]): An entity dataframe is a collection of rows containing all entity
                columns (e.g., customer_id, driver_id) on which features need to be joined, as well as a event_timestamp
                column used to ensure point-in-time correctness. Either a Pandas DataFrame can be provided or a string
                SQL query. The query must be of a format supported by the configured offline store (e.g., BigQuery)
            features: The list of features that should be retrieved from the offline store. These features can be
                specified either as a list of string feature references or as a feature service. String feature
                references must have format "feature_view:feature", e.g. "customer_fv:daily_transactions".
            full_feature_names: If True, feature names will be prefixed with the corresponding feature view name,
                changing them from the format "feature" to "feature_view__feature" (e.g. "daily_transactions"
                changes to "customer_fv__daily_transactions").

        Returns:
            RetrievalJob which can be used to materialize the results.

        Raises:
            ValueError: Both or neither of features and feature_refs are specified.

        Examples:
            Retrieve historical features from a local offline store.

            >>> from feast import FeatureStore, RepoConfig
            >>> import pandas as pd
            >>> fs = FeatureStore(repo_path="feature_repo")
            >>> entity_df = pd.DataFrame.from_dict(
            ...     {
            ...         "driver_id": [1001, 1002],
            ...         "event_timestamp": [
            ...             datetime(2021, 4, 12, 10, 59, 42),
            ...             datetime(2021, 4, 12, 8, 12, 10),
            ...         ],
            ...     }
            ... )
            >>> retrieval_job = fs.get_historical_features(
            ...     entity_df=entity_df,
            ...     features=[
            ...         "driver_hourly_stats:conv_rate",
            ...         "driver_hourly_stats:acc_rate",
            ...         "driver_hourly_stats:avg_daily_trips",
            ...     ],
            ... )
            >>> feature_data = retrieval_job.to_df()
        """
        _feature_refs = self._get_features(features)
        (
            all_feature_views,
            all_request_feature_views,
            all_on_demand_feature_views,
        ) = self._get_feature_views_to_use(features)

        if all_request_feature_views:
            warnings.warn(
                "Request feature view is deprecated. "
                "Please use request data source instead",
                DeprecationWarning,
            )

        # TODO(achal): _group_feature_refs returns the on demand feature views, but it's not passed into the provider.
        # This is a weird interface quirk - we should revisit the `get_historical_features` to
        # pass in the on demand feature views as well.
        fvs, odfvs, request_fvs, request_fv_refs = _group_feature_refs(
            _feature_refs,
            all_feature_views,
            all_request_feature_views,
            all_on_demand_feature_views,
        )
        feature_views = list(view for view, _ in fvs)
        on_demand_feature_views = list(view for view, _ in odfvs)
        request_feature_views = list(view for view, _ in request_fvs)

        set_usage_attribute("odfv", bool(on_demand_feature_views))
        set_usage_attribute("request_fv", bool(request_feature_views))

        # Check that the right request data is present in the entity_df
        if type(entity_df) == pd.DataFrame:
            entity_df = utils.make_df_tzaware(cast(pd.DataFrame, entity_df))
            for fv in request_feature_views:
                for feature in fv.features:
                    if feature.name not in entity_df.columns:
                        raise RequestDataNotFoundInEntityDfException(
                            feature_name=feature.name, feature_view_name=fv.name
                        )
            for odfv in on_demand_feature_views:
                odfv_request_data_schema = odfv.get_request_data_schema()
                for feature_name in odfv_request_data_schema.keys():
                    if feature_name not in entity_df.columns:
                        raise RequestDataNotFoundInEntityDfException(
                            feature_name=feature_name,
                            feature_view_name=odfv.name,
                        )

        _validate_feature_refs(_feature_refs, full_feature_names)
        # Drop refs that refer to RequestFeatureViews since they don't need to be fetched and
        # already exist in the entity_df
        _feature_refs = [ref for ref in _feature_refs if ref not in request_fv_refs]
        provider = self._get_provider()

        job = provider.get_historical_features(
            self.config,
            feature_views,
            _feature_refs,
            entity_df,
            self._registry,
            self.project,
            full_feature_names,
        )

        return job

    @log_exceptions_and_usage
    def create_saved_dataset(
        self,
        from_: RetrievalJob,
        name: str,
        storage: SavedDatasetStorage,
        tags: Optional[Dict[str, str]] = None,
        feature_service: Optional[FeatureService] = None,
    ) -> SavedDataset:
        """
        Execute provided retrieval job and persist its outcome in given storage.
        Storage type (eg, BigQuery or Redshift) must be the same as globally configured offline store.
        After data successfully persisted saved dataset object with dataset metadata is committed to the registry.
        Name for the saved dataset should be unique within project, since it's possible to overwrite previously stored dataset
        with the same name.

        Returns:
            SavedDataset object with attached RetrievalJob

        Raises:
            ValueError if given retrieval job doesn't have metadata
        """
        warnings.warn(
            "Saving dataset is an experimental feature. "
            "This API is unstable and it could and most probably will be changed in the future. "
            "We do not guarantee that future changes will maintain backward compatibility.",
            RuntimeWarning,
        )

        if not from_.metadata:
            raise ValueError(
                f"The RetrievalJob {type(from_)} must implement the metadata property."
            )

        dataset = SavedDataset(
            name=name,
            features=from_.metadata.features,
            join_keys=from_.metadata.keys,
            full_feature_names=from_.full_feature_names,
            storage=storage,
            tags=tags,
            feature_service_name=feature_service.name if feature_service else None,
        )

        dataset.min_event_timestamp = from_.metadata.min_event_timestamp
        dataset.max_event_timestamp = from_.metadata.max_event_timestamp

        from_.persist(storage)

        dataset = dataset.with_retrieval_job(
            self._get_provider().retrieve_saved_dataset(
                config=self.config, dataset=dataset
            )
        )

        self._registry.apply_saved_dataset(dataset, self.project, commit=True)
        return dataset

    @log_exceptions_and_usage
    def get_saved_dataset(self, name: str) -> SavedDataset:
        """
        Find a saved dataset in the registry by provided name and
        create a retrieval job to pull whole dataset from storage (offline store).

        If dataset couldn't be found by provided name SavedDatasetNotFound exception will be raised.

        Data will be retrieved from globally configured offline store.

        Returns:
            SavedDataset with RetrievalJob attached

        Raises:
            SavedDatasetNotFound
        """
        warnings.warn(
            "Retrieving datasets is an experimental feature. "
            "This API is unstable and it could and most probably will be changed in the future. "
            "We do not guarantee that future changes will maintain backward compatibility.",
            RuntimeWarning,
        )

        dataset = self._registry.get_saved_dataset(name, self.project)
        provider = self._get_provider()

        retrieval_job = provider.retrieve_saved_dataset(
            config=self.config, dataset=dataset
        )
        return dataset.with_retrieval_job(retrieval_job)

    @log_exceptions_and_usage
    def materialize_incremental(
        self,
        end_date: datetime,
        feature_views: Optional[List[str]] = None,
    ) -> None:
        """
        Materialize incremental new data from the offline store into the online store.

        This method loads incremental new feature data up to the specified end time from either
        the specified feature views, or all feature views if none are specified,
        into the online store where it is available for online serving. The start time of
        the interval materialized is either the most recent end time of a prior materialization or
        (now - ttl) if no such prior materialization exists.

        Args:
            end_date (datetime): End date for time range of data to materialize into the online store
            feature_views (List[str]): Optional list of feature view names. If selected, will only run
                materialization for the specified feature views.

        Raises:
            Exception: A feature view being materialized does not have a TTL set.

        Examples:
            Materialize all features into the online store up to 5 minutes ago.

            >>> from feast import FeatureStore, RepoConfig
            >>> from datetime import datetime, timedelta
            >>> fs = FeatureStore(repo_path="feature_repo")
            >>> fs.materialize_incremental(end_date=datetime.utcnow() - timedelta(minutes=5))
            Materializing...
            <BLANKLINE>
            ...
        """
        feature_views_to_materialize = self._get_feature_views_to_materialize(
            feature_views
        )
        _print_materialization_log(
            None,
            end_date,
            len(feature_views_to_materialize),
            self.config.online_store.type,
        )
        # TODO paging large loads
        for feature_view in feature_views_to_materialize:
            start_date = feature_view.most_recent_end_time
            if start_date is None:
                if feature_view.ttl is None:
                    raise Exception(
                        f"No start time found for feature view {feature_view.name}. materialize_incremental() requires"
                        f" either a ttl to be set or for materialize() to have been run at least once."
                    )
                elif feature_view.ttl.total_seconds() > 0:
                    start_date = datetime.utcnow() - feature_view.ttl
                else:
                    # TODO(felixwang9817): Find the earliest timestamp for this specific feature
                    # view from the offline store, and set the start date to that timestamp.
                    print(
                        f"Since the ttl is 0 for feature view {Style.BRIGHT + Fore.GREEN}{feature_view.name}{Style.RESET_ALL}, "
                        "the start date will be set to 1 year before the current time."
                    )
                    start_date = datetime.utcnow() - timedelta(weeks=52)
            provider = self._get_provider()
            print(
                f"{Style.BRIGHT + Fore.GREEN}{feature_view.name}{Style.RESET_ALL}"
                f" from {Style.BRIGHT + Fore.GREEN}{start_date.replace(microsecond=0).astimezone()}{Style.RESET_ALL}"
                f" to {Style.BRIGHT + Fore.GREEN}{end_date.replace(microsecond=0).astimezone()}{Style.RESET_ALL}:"
            )

            def tqdm_builder(length):
                return tqdm(total=length, ncols=100)

            start_date = utils.make_tzaware(start_date)
            end_date = utils.make_tzaware(end_date)

            provider.materialize_single_feature_view(
                config=self.config,
                feature_view=feature_view,
                start_date=start_date,
                end_date=end_date,
                registry=self._registry,
                project=self.project,
                tqdm_builder=tqdm_builder,
            )

            self._registry.apply_materialization(
                feature_view,
                self.project,
                start_date,
                end_date,
            )

    @log_exceptions_and_usage
    def materialize(
        self,
        start_date: datetime,
        end_date: datetime,
        feature_views: Optional[List[str]] = None,
    ) -> None:
        """
        Materialize data from the offline store into the online store.

        This method loads feature data in the specified interval from either
        the specified feature views, or all feature views if none are specified,
        into the online store where it is available for online serving.

        Args:
            start_date (datetime): Start date for time range of data to materialize into the online store
            end_date (datetime): End date for time range of data to materialize into the online store
            feature_views (List[str]): Optional list of feature view names. If selected, will only run
                materialization for the specified feature views.

        Examples:
            Materialize all features into the online store over the interval
            from 3 hours ago to 10 minutes ago.
            >>> from feast import FeatureStore, RepoConfig
            >>> from datetime import datetime, timedelta
            >>> fs = FeatureStore(repo_path="feature_repo")
            >>> fs.materialize(
            ...     start_date=datetime.utcnow() - timedelta(hours=3), end_date=datetime.utcnow() - timedelta(minutes=10)
            ... )
            Materializing...
            <BLANKLINE>
            ...
        """
        if utils.make_tzaware(start_date) > utils.make_tzaware(end_date):
            raise ValueError(
                f"The given start_date {start_date} is greater than the given end_date {end_date}."
            )

        feature_views_to_materialize = self._get_feature_views_to_materialize(
            feature_views
        )
        _print_materialization_log(
            start_date,
            end_date,
            len(feature_views_to_materialize),
            self.config.online_store.type,
        )
        # TODO paging large loads
        for feature_view in feature_views_to_materialize:
            provider = self._get_provider()
            print(f"{Style.BRIGHT + Fore.GREEN}{feature_view.name}{Style.RESET_ALL}:")

            def tqdm_builder(length):
                return tqdm(total=length, ncols=100)

            start_date = utils.make_tzaware(start_date)
            end_date = utils.make_tzaware(end_date)

            provider.materialize_single_feature_view(
                config=self.config,
                feature_view=feature_view,
                start_date=start_date,
                end_date=end_date,
                registry=self._registry,
                project=self.project,
                tqdm_builder=tqdm_builder,
            )

            self._registry.apply_materialization(
                feature_view,
                self.project,
                start_date,
                end_date,
            )

    @log_exceptions_and_usage
    def push(
        self,
        push_source_name: str,
        df: pd.DataFrame,
        allow_registry_cache: bool = True,
        to: PushMode = PushMode.ONLINE,
    ):
        """
        Push features to a push source. This updates all the feature views that have the push source as stream source.

        Args:
            push_source_name: The name of the push source we want to push data to.
            df: The data being pushed.
            allow_registry_cache: Whether to allow cached versions of the registry.
            to: Whether to push to online or offline store. Defaults to online store only.
        """
        warnings.warn(
            "Push source is an experimental feature. "
            "This API is unstable and it could and might change in the future. "
            "We do not guarantee that future changes will maintain backward compatibility.",
            RuntimeWarning,
        )
        from feast.data_source import PushSource

        all_fvs = self.list_feature_views(allow_cache=allow_registry_cache)
        all_fvs += self.list_stream_feature_views(allow_cache=allow_registry_cache)

        fvs_with_push_sources = {
            fv
            for fv in all_fvs
            if (
                fv.stream_source is not None
                and isinstance(fv.stream_source, PushSource)
                and fv.stream_source.name == push_source_name
            )
        }

        for fv in fvs_with_push_sources:
            if to == PushMode.ONLINE or to == PushMode.ONLINE_AND_OFFLINE:
                self.write_to_online_store(
                    fv.name, df, allow_registry_cache=allow_registry_cache
                )
            if to == PushMode.OFFLINE or to == PushMode.ONLINE_AND_OFFLINE:
                self.write_to_offline_store(
                    fv.name, df, allow_registry_cache=allow_registry_cache
                )

    @log_exceptions_and_usage
    def write_to_online_store(
        self,
        feature_view_name: str,
        df: pd.DataFrame,
        allow_registry_cache: bool = True,
    ):
        """
        ingests data directly into the Online store
        """
        # TODO: restrict this to work with online StreamFeatureViews and validate the FeatureView type
        try:
            feature_view = self.get_stream_feature_view(
                feature_view_name, allow_registry_cache=allow_registry_cache
            )
        except FeatureViewNotFoundException:
            feature_view = self.get_feature_view(
                feature_view_name, allow_registry_cache=allow_registry_cache
            )
        entities = []
        for entity_name in feature_view.entities:
            entities.append(
                self.get_entity(entity_name, allow_registry_cache=allow_registry_cache)
            )
        provider = self._get_provider()
        provider.ingest_df(feature_view, entities, df)

    @log_exceptions_and_usage
    def write_to_offline_store(
        self,
        feature_view_name: str,
        df: pd.DataFrame,
        allow_registry_cache: bool = True,
        reorder_columns: bool = True,
    ):
        """
        Persists the dataframe directly into the batch data source for the given feature view.

        Fails if the dataframe columns do not match the columns of the batch data source. Optionally
        reorders the columns of the dataframe to match.
        """
        # TODO: restrict this to work with online StreamFeatureViews and validate the FeatureView type
        try:
            feature_view = self.get_stream_feature_view(
                feature_view_name, allow_registry_cache=allow_registry_cache
            )
        except FeatureViewNotFoundException:
            feature_view = self.get_feature_view(
                feature_view_name, allow_registry_cache=allow_registry_cache
            )

        # Get columns of the batch source and the input dataframe.
        column_names_and_types = (
            feature_view.batch_source.get_table_column_names_and_types(self.config)
        )
        source_columns = [column for column, _ in column_names_and_types]
        input_columns = df.columns.values.tolist()

        if set(input_columns) != set(source_columns):
            raise ValueError(
                f"The input dataframe has columns {set(input_columns)} but the batch source has columns {set(source_columns)}."
            )

        if reorder_columns:
            df = df.reindex(columns=source_columns)

        table = pa.Table.from_pandas(df)
        provider = self._get_provider()
        provider.ingest_df_to_offline_store(feature_view, table)

    @log_exceptions_and_usage
    def get_online_features(
        self,
        features: Union[List[str], FeatureService],
        entity_rows: List[Dict[str, Any]],
        full_feature_names: bool = False,
    ) -> OnlineResponse:
        """
        Retrieves the latest online feature data.

        Note: This method will download the full feature registry the first time it is run. If you are using a
        remote registry like GCS or S3 then that may take a few seconds. The registry remains cached up to a TTL
        duration (which can be set to infinity). If the cached registry is stale (more time than the TTL has
        passed), then a new registry will be downloaded synchronously by this method. This download may
        introduce latency to online feature retrieval. In order to avoid synchronous downloads, please call
        refresh_registry() prior to the TTL being reached. Remember it is possible to set the cache TTL to
        infinity (cache forever).

        Args:
            features: The list of features that should be retrieved from the online store. These features can be
                specified either as a list of string feature references or as a feature service. String feature
                references must have format "feature_view:feature", e.g. "customer_fv:daily_transactions".
            entity_rows: A list of dictionaries where each key-value is an entity-name, entity-value pair.
            full_feature_names: If True, feature names will be prefixed with the corresponding feature view name,
                changing them from the format "feature" to "feature_view__feature" (e.g. "daily_transactions"
                changes to "customer_fv__daily_transactions").

        Returns:
            OnlineResponse containing the feature data in records.

        Raises:
            Exception: No entity with the specified name exists.

        Examples:
            Retrieve online features from an online store.

            >>> from feast import FeatureStore, RepoConfig
            >>> fs = FeatureStore(repo_path="feature_repo")
            >>> online_response = fs.get_online_features(
            ...     features=[
            ...         "driver_hourly_stats:conv_rate",
            ...         "driver_hourly_stats:acc_rate",
            ...         "driver_hourly_stats:avg_daily_trips",
            ...     ],
            ...     entity_rows=[{"driver_id": 1001}, {"driver_id": 1002}, {"driver_id": 1003}, {"driver_id": 1004}],
            ... )
            >>> online_response_dict = online_response.to_dict()
        """
        columnar: Dict[str, List[Any]] = {k: [] for k in entity_rows[0].keys()}
        for entity_row in entity_rows:
            for key, value in entity_row.items():
                try:
                    columnar[key].append(value)
                except KeyError as e:
                    raise ValueError("All entity_rows must have the same keys.") from e

        return self._get_online_features(
            features=features,
            entity_values=columnar,
            full_feature_names=full_feature_names,
            native_entity_values=True,
        )

    def _lazy_init_go_server(self):
        """Lazily initialize self._go_server if it hasn't been initialized before."""
        from feast.embedded_go.online_features_service import (
            EmbeddedOnlineFeatureServer,
        )

        # Lazily start the go server on the first request
        if self._go_server is None:
            self._go_server = EmbeddedOnlineFeatureServer(
                str(self.repo_path.absolute()), self.config, self
            )

    def _get_online_features(
        self,
        features: Union[List[str], FeatureService],
        entity_values: Mapping[
            str, Union[Sequence[Any], Sequence[Value], RepeatedValue]
        ],
        full_feature_names: bool = False,
        native_entity_values: bool = True,
    ):
        # Extract Sequence from RepeatedValue Protobuf.
        entity_value_lists: Dict[str, Union[List[Any], List[Value]]] = {
            k: list(v) if isinstance(v, Sequence) else list(v.val)
            for k, v in entity_values.items()
        }

        # If the embedded Go code is enabled, send request to it instead of going through regular Python logic.
        if self.config.go_feature_retrieval:
            self._lazy_init_go_server()

            entity_native_values: Dict[str, List[Any]]
            if not native_entity_values:
                # Convert proto types to native types since Go feature server currently
                # only handles native types.
                # TODO(felixwang9817): Remove this logic once native types are supported.
                entity_native_values = {
                    k: [
                        feast_value_type_to_python_type(proto_value)
                        for proto_value in v
                    ]
                    for k, v in entity_value_lists.items()
                }
            else:
                entity_native_values = entity_value_lists

            return self._go_server.get_online_features(
                features_refs=features if isinstance(features, list) else [],
                feature_service=features
                if isinstance(features, FeatureService)
                else None,
                entities=entity_native_values,
                request_data={},  # TODO: add request data parameter to public API
                full_feature_names=full_feature_names,
            )

        _feature_refs = self._get_features(features, allow_cache=True)
        (
            requested_feature_views,
            requested_request_feature_views,
            requested_on_demand_feature_views,
        ) = self._get_feature_views_to_use(
            features=features, allow_cache=True, hide_dummy_entity=False
        )

        if requested_request_feature_views:
            warnings.warn(
                "Request feature view is deprecated. "
                "Please use request data source instead",
                DeprecationWarning,
            )

        (
            entity_name_to_join_key_map,
            entity_type_map,
            join_keys_set,
        ) = self._get_entity_maps(requested_feature_views)

        entity_proto_values: Dict[str, List[Value]]
        if native_entity_values:
            # Convert values to Protobuf once.
            entity_proto_values = {
                k: python_values_to_proto_values(
                    v, entity_type_map.get(k, ValueType.UNKNOWN)
                )
                for k, v in entity_value_lists.items()
            }
        else:
            entity_proto_values = entity_value_lists

        num_rows = _validate_entity_values(entity_proto_values)
        _validate_feature_refs(_feature_refs, full_feature_names)
        (
            grouped_refs,
            grouped_odfv_refs,
            grouped_request_fv_refs,
            _,
        ) = _group_feature_refs(
            _feature_refs,
            requested_feature_views,
            requested_request_feature_views,
            requested_on_demand_feature_views,
        )
        set_usage_attribute("odfv", bool(grouped_odfv_refs))
        set_usage_attribute("request_fv", bool(grouped_request_fv_refs))

        # All requested features should be present in the result.
        requested_result_row_names = {
            feat_ref.replace(":", "__") for feat_ref in _feature_refs
        }
        if not full_feature_names:
            requested_result_row_names = {
                name.rpartition("__")[-1] for name in requested_result_row_names
            }

        feature_views = list(view for view, _ in grouped_refs)

        needed_request_data, needed_request_fv_features = self.get_needed_request_data(
            grouped_odfv_refs, grouped_request_fv_refs
        )

        join_key_values: Dict[str, List[Value]] = {}
        request_data_features: Dict[str, List[Value]] = {}
        # Entity rows may be either entities or request data.
        for join_key_or_entity_name, values in entity_proto_values.items():
            # Found request data
            if (
                join_key_or_entity_name in needed_request_data
                or join_key_or_entity_name in needed_request_fv_features
            ):
                if join_key_or_entity_name in needed_request_fv_features:
                    # If the data was requested as a feature then
                    # make sure it appears in the result.
                    requested_result_row_names.add(join_key_or_entity_name)
                request_data_features[join_key_or_entity_name] = values
            else:
                if join_key_or_entity_name in join_keys_set:
                    join_key = join_key_or_entity_name
                else:
                    try:
                        join_key = entity_name_to_join_key_map[join_key_or_entity_name]
                    except KeyError:
                        raise EntityNotFoundException(
                            join_key_or_entity_name, self.project
                        )
                    else:
                        warnings.warn(
                            "Using entity name is deprecated. Use join_key instead."
                        )

                # All join keys should be returned in the result.
                requested_result_row_names.add(join_key)
                join_key_values[join_key] = values

        self.ensure_request_data_values_exist(
            needed_request_data, needed_request_fv_features, request_data_features
        )

        # Populate online features response proto with join keys and request data features
        online_features_response = GetOnlineFeaturesResponse(results=[])
        self._populate_result_rows_from_columnar(
            online_features_response=online_features_response,
            data=dict(**join_key_values, **request_data_features),
        )

        # Add the Entityless case after populating result rows to avoid having to remove
        # it later.
        entityless_case = DUMMY_ENTITY_NAME in [
            entity_name
            for feature_view in feature_views
            for entity_name in feature_view.entities
        ]
        if entityless_case:
            join_key_values[DUMMY_ENTITY_ID] = python_values_to_proto_values(
                [DUMMY_ENTITY_VAL] * num_rows, DUMMY_ENTITY.value_type
            )

        provider = self._get_provider()
        for table, requested_features in grouped_refs:
            # Get the correct set of entity values with the correct join keys.
            table_entity_values, idxs = self._get_unique_entities(
                table,
                join_key_values,
                entity_name_to_join_key_map,
            )

            # Fetch feature data for the minimum set of Entities.
            feature_data = self._read_from_online_store(
                table_entity_values,
                provider,
                requested_features,
                table,
            )

            # Populate the result_rows with the Features from the OnlineStore inplace.
            self._populate_response_from_feature_data(
                feature_data,
                idxs,
                online_features_response,
                full_feature_names,
                requested_features,
                table,
            )

        if grouped_odfv_refs:
            self._augment_response_with_on_demand_transforms(
                online_features_response,
                _feature_refs,
                requested_on_demand_feature_views,
                full_feature_names,
            )

        self._drop_unneeded_columns(
            online_features_response, requested_result_row_names
        )
        return OnlineResponse(online_features_response)

    @staticmethod
    def _get_columnar_entity_values(
        rowise: Optional[List[Dict[str, Any]]], columnar: Optional[Dict[str, List[Any]]]
    ) -> Dict[str, List[Any]]:
        if (rowise is None and columnar is None) or (
            rowise is not None and columnar is not None
        ):
            raise ValueError(
                "Exactly one of `columnar_entity_values` and `rowise_entity_values` must be set."
            )

        if rowise is not None:
            # Convert entity_rows from rowise to columnar.
            res = defaultdict(list)
            for entity_row in rowise:
                for key, value in entity_row.items():
                    res[key].append(value)
            return res
        return cast(Dict[str, List[Any]], columnar)

    def _get_entity_maps(
        self, feature_views
    ) -> Tuple[Dict[str, str], Dict[str, ValueType], Set[str]]:
        # TODO(felixwang9817): Support entities that have different types for different feature views.
        entities = self._list_entities(allow_cache=True, hide_dummy_entity=False)
        entity_name_to_join_key_map: Dict[str, str] = {}
        entity_type_map: Dict[str, ValueType] = {}
        for entity in entities:
            entity_name_to_join_key_map[entity.name] = entity.join_key
        for feature_view in feature_views:
            for entity_name in feature_view.entities:
                entity = self._registry.get_entity(
                    entity_name, self.project, allow_cache=True
                )
                # User directly uses join_key as the entity reference in the entity_rows for the
                # entity mapping case.
                entity_name = feature_view.projection.join_key_map.get(
                    entity.join_key, entity.name
                )
                join_key = feature_view.projection.join_key_map.get(
                    entity.join_key, entity.join_key
                )
                entity_name_to_join_key_map[entity_name] = join_key
            for entity_column in feature_view.entity_columns:
                entity_type_map[
                    entity_column.name
                ] = entity_column.dtype.to_value_type()

        return (
            entity_name_to_join_key_map,
            entity_type_map,
            set(entity_name_to_join_key_map.values()),
        )

    @staticmethod
    def _get_table_entity_values(
        table: FeatureView,
        entity_name_to_join_key_map: Dict[str, str],
        join_key_proto_values: Dict[str, List[Value]],
    ) -> Dict[str, List[Value]]:
        # The correct join_keys expected by the OnlineStore for this Feature View.
        table_join_keys = [
            entity_name_to_join_key_map[entity_name] for entity_name in table.entities
        ]

        # If the FeatureView has a Projection then the join keys may be aliased.
        alias_to_join_key_map = {v: k for k, v in table.projection.join_key_map.items()}

        # Subset to columns which are relevant to this FeatureView and
        # give them the correct names.
        entity_values = {
            alias_to_join_key_map.get(k, k): v
            for k, v in join_key_proto_values.items()
            if alias_to_join_key_map.get(k, k) in table_join_keys
        }
        return entity_values

    @staticmethod
    def _populate_result_rows_from_columnar(
        online_features_response: GetOnlineFeaturesResponse,
        data: Dict[str, List[Value]],
    ):
        timestamp = Timestamp()  # Only initialize this timestamp once.
        # Add more values to the existing result rows
        for feature_name, feature_values in data.items():
            online_features_response.metadata.feature_names.val.append(feature_name)
            online_features_response.results.append(
                GetOnlineFeaturesResponse.FeatureVector(
                    values=feature_values,
                    statuses=[FieldStatus.PRESENT] * len(feature_values),
                    event_timestamps=[timestamp] * len(feature_values),
                )
            )

    @staticmethod
    def get_needed_request_data(
        grouped_odfv_refs: List[Tuple[OnDemandFeatureView, List[str]]],
        grouped_request_fv_refs: List[Tuple[RequestFeatureView, List[str]]],
    ) -> Tuple[Set[str], Set[str]]:
        needed_request_data: Set[str] = set()
        needed_request_fv_features: Set[str] = set()
        for odfv, _ in grouped_odfv_refs:
            odfv_request_data_schema = odfv.get_request_data_schema()
            needed_request_data.update(odfv_request_data_schema.keys())
        for request_fv, _ in grouped_request_fv_refs:
            for feature in request_fv.features:
                needed_request_fv_features.add(feature.name)
        return needed_request_data, needed_request_fv_features

    @staticmethod
    def ensure_request_data_values_exist(
        needed_request_data: Set[str],
        needed_request_fv_features: Set[str],
        request_data_features: Dict[str, List[Any]],
    ):
        if len(needed_request_data) + len(needed_request_fv_features) != len(
            request_data_features.keys()
        ):
            missing_features = [
                x
                for x in itertools.chain(
                    needed_request_data, needed_request_fv_features
                )
                if x not in request_data_features
            ]
            raise RequestDataNotFoundInEntityRowsException(
                feature_names=missing_features
            )

    def _get_unique_entities(
        self,
        table: FeatureView,
        join_key_values: Dict[str, List[Value]],
        entity_name_to_join_key_map: Dict[str, str],
    ) -> Tuple[Tuple[Dict[str, Value], ...], Tuple[List[int], ...]]:
        """Return the set of unique composite Entities for a Feature View and the indexes at which they appear.

        This method allows us to query the OnlineStore for data we need only once
        rather than requesting and processing data for the same combination of
        Entities multiple times.
        """
        # Get the correct set of entity values with the correct join keys.
        table_entity_values = self._get_table_entity_values(
            table,
            entity_name_to_join_key_map,
            join_key_values,
        )

        # Convert back to rowise.
        keys = table_entity_values.keys()
        # Sort the rowise data to allow for grouping but keep original index. This lambda is
        # sufficient as Entity types cannot be complex (ie. lists).
        rowise = list(enumerate(zip(*table_entity_values.values())))
        rowise.sort(
            key=lambda row: tuple(getattr(x, x.WhichOneof("val")) for x in row[1])
        )

        # Identify unique entities and the indexes at which they occur.
        unique_entities: Tuple[Dict[str, Value], ...]
        indexes: Tuple[List[int], ...]
        unique_entities, indexes = tuple(
            zip(
                *[
                    (dict(zip(keys, k)), [_[0] for _ in g])
                    for k, g in itertools.groupby(rowise, key=lambda x: x[1])
                ]
            )
        )
        return unique_entities, indexes

    def _read_from_online_store(
        self,
        entity_rows: Iterable[Mapping[str, Value]],
        provider: Provider,
        requested_features: List[str],
        table: FeatureView,
    ) -> List[Tuple[List[Timestamp], List["FieldStatus.ValueType"], List[Value]]]:
        """Read and process data from the OnlineStore for a given FeatureView.

        This method guarantees that the order of the data in each element of the
        List returned is the same as the order of `requested_features`.

        This method assumes that `provider.online_read` returns data for each
        combination of Entities in `entity_rows` in the same order as they
        are provided.
        """
        # Instantiate one EntityKeyProto per Entity.
        entity_key_protos = [
            EntityKeyProto(join_keys=row.keys(), entity_values=row.values())
            for row in entity_rows
        ]

        # Fetch data for Entities.
        read_rows = provider.online_read(
            config=self.config,
            table=table,
            entity_keys=entity_key_protos,
            requested_features=requested_features,
        )

        # Each row is a set of features for a given entity key. We only need to convert
        # the data to Protobuf once.
        null_value = Value()
        read_row_protos = []
        for read_row in read_rows:
            row_ts_proto = Timestamp()
            row_ts, feature_data = read_row
            # TODO (Ly): reuse whatever timestamp if row_ts is None?
            if row_ts is not None:
                row_ts_proto.FromDatetime(row_ts)
            event_timestamps = [row_ts_proto] * len(requested_features)
            if feature_data is None:
                statuses = [FieldStatus.NOT_FOUND] * len(requested_features)
                values = [null_value] * len(requested_features)
            else:
                statuses = []
                values = []
                for feature_name in requested_features:
                    # Make sure order of data is the same as requested_features.
                    if feature_name not in feature_data:
                        statuses.append(FieldStatus.NOT_FOUND)
                        values.append(null_value)
                    else:
                        statuses.append(FieldStatus.PRESENT)
                        values.append(feature_data[feature_name])
            read_row_protos.append((event_timestamps, statuses, values))
        return read_row_protos

    @staticmethod
    def _populate_response_from_feature_data(
        feature_data: Iterable[
            Tuple[
                Iterable[Timestamp], Iterable["FieldStatus.ValueType"], Iterable[Value]
            ]
        ],
        indexes: Iterable[List[int]],
        online_features_response: GetOnlineFeaturesResponse,
        full_feature_names: bool,
        requested_features: Iterable[str],
        table: FeatureView,
    ):
        """Populate the GetOnlineFeaturesResponse with feature data.

        This method assumes that `_read_from_online_store` returns data for each
        combination of Entities in `entity_rows` in the same order as they
        are provided.

        Args:
            feature_data: A list of data in Protobuf form which was retrieved from the OnlineStore.
            indexes: A list of indexes which should be the same length as `feature_data`. Each list
                of indexes corresponds to a set of result rows in `online_features_response`.
            online_features_response: The object to populate.
            full_feature_names: A boolean that provides the option to add the feature view prefixes to the feature names,
                changing them from the format "feature" to "feature_view__feature" (e.g., "daily_transactions" changes to
                "customer_fv__daily_transactions").
            requested_features: The names of the features in `feature_data`. This should be ordered in the same way as the
                data in `feature_data`.
            table: The FeatureView that `feature_data` was retrieved from.
        """
        # Add the feature names to the response.
        requested_feature_refs = [
            f"{table.projection.name_to_use()}__{feature_name}"
            if full_feature_names
            else feature_name
            for feature_name in requested_features
        ]
        online_features_response.metadata.feature_names.val.extend(
            requested_feature_refs
        )

        timestamps, statuses, values = zip(*feature_data)

        # Populate the result with data fetched from the OnlineStore
        # which is guaranteed to be aligned with `requested_features`.
        for (
            feature_idx,
            (timestamp_vector, statuses_vector, values_vector),
        ) in enumerate(zip(zip(*timestamps), zip(*statuses), zip(*values))):
            online_features_response.results.append(
                GetOnlineFeaturesResponse.FeatureVector(
                    values=apply_list_mapping(values_vector, indexes),
                    statuses=apply_list_mapping(statuses_vector, indexes),
                    event_timestamps=apply_list_mapping(timestamp_vector, indexes),
                )
            )

    @staticmethod
    def _augment_response_with_on_demand_transforms(
        online_features_response: GetOnlineFeaturesResponse,
        feature_refs: List[str],
        requested_on_demand_feature_views: List[OnDemandFeatureView],
        full_feature_names: bool,
    ):
        """Computes on demand feature values and adds them to the result rows.

        Assumes that 'online_features_response' already contains the necessary request data and input feature
        views for the on demand feature views. Unneeded feature values such as request data and
        unrequested input feature views will be removed from 'online_features_response'.

        Args:
            online_features_response: Protobuf object to populate
            feature_refs: List of all feature references to be returned.
            requested_on_demand_feature_views: List of all odfvs that have been requested.
            full_feature_names: A boolean that provides the option to add the feature view prefixes to the feature names,
                changing them from the format "feature" to "feature_view__feature" (e.g., "daily_transactions" changes to
                "customer_fv__daily_transactions").
        """
        requested_odfv_map = {
            odfv.name: odfv for odfv in requested_on_demand_feature_views
        }
        requested_odfv_feature_names = requested_odfv_map.keys()

        odfv_feature_refs = defaultdict(list)
        for feature_ref in feature_refs:
            view_name, feature_name = feature_ref.split(":")
            if view_name in requested_odfv_feature_names:
                odfv_feature_refs[view_name].append(
                    f"{requested_odfv_map[view_name].projection.name_to_use()}__{feature_name}"
                    if full_feature_names
                    else feature_name
                )

        initial_response = OnlineResponse(online_features_response)
        initial_response_df = initial_response.to_df()

        # Apply on demand transformations and augment the result rows
        odfv_result_names = set()
        for odfv_name, _feature_refs in odfv_feature_refs.items():
            odfv = requested_odfv_map[odfv_name]
            transformed_features_df = odfv.get_transformed_features_df(
                initial_response_df,
                full_feature_names,
            )
            selected_subset = [
                f for f in transformed_features_df.columns if f in _feature_refs
            ]

            proto_values = [
                python_values_to_proto_values(
                    transformed_features_df[feature].values, ValueType.UNKNOWN
                )
                for feature in selected_subset
            ]

            odfv_result_names |= set(selected_subset)

            online_features_response.metadata.feature_names.val.extend(selected_subset)
            for feature_idx in range(len(selected_subset)):
                online_features_response.results.append(
                    GetOnlineFeaturesResponse.FeatureVector(
                        values=proto_values[feature_idx],
                        statuses=[FieldStatus.PRESENT] * len(proto_values[feature_idx]),
                        event_timestamps=[Timestamp()] * len(proto_values[feature_idx]),
                    )
                )

    @staticmethod
    def _drop_unneeded_columns(
        online_features_response: GetOnlineFeaturesResponse,
        requested_result_row_names: Set[str],
    ):
        """
        Unneeded feature values such as request data and unrequested input feature views will
        be removed from 'online_features_response'.

        Args:
            online_features_response: Protobuf object to populate
            requested_result_row_names: Fields from 'result_rows' that have been requested, and
                    therefore should not be dropped.
        """
        # Drop values that aren't needed
        unneeded_feature_indices = [
            idx
            for idx, val in enumerate(
                online_features_response.metadata.feature_names.val
            )
            if val not in requested_result_row_names
        ]

        for idx in reversed(unneeded_feature_indices):
            del online_features_response.metadata.feature_names.val[idx]
            del online_features_response.results[idx]

    def _get_feature_views_to_use(
        self,
        features: Optional[Union[List[str], FeatureService]],
        allow_cache=False,
        hide_dummy_entity: bool = True,
    ) -> Tuple[List[FeatureView], List[RequestFeatureView], List[OnDemandFeatureView]]:

        fvs = {
            fv.name: fv
            for fv in [
                *self._list_feature_views(allow_cache, hide_dummy_entity),
                *self._registry.list_stream_feature_views(
                    project=self.project, allow_cache=allow_cache
                ),
            ]
        }

        request_fvs = {
            fv.name: fv
            for fv in self._registry.list_request_feature_views(
                project=self.project, allow_cache=allow_cache
            )
        }

        od_fvs = {
            fv.name: fv
            for fv in self._registry.list_on_demand_feature_views(
                project=self.project, allow_cache=allow_cache
            )
        }

        if isinstance(features, FeatureService):
            fvs_to_use, request_fvs_to_use, od_fvs_to_use = [], [], []
            for fv_name, projection in [
                (projection.name, projection)
                for projection in features.feature_view_projections
            ]:
                if fv_name in fvs:
                    fvs_to_use.append(
                        fvs[fv_name].with_projection(copy.copy(projection))
                    )
                elif fv_name in request_fvs:
                    request_fvs_to_use.append(
                        request_fvs[fv_name].with_projection(copy.copy(projection))
                    )
                elif fv_name in od_fvs:
                    odfv = od_fvs[fv_name].with_projection(copy.copy(projection))
                    od_fvs_to_use.append(odfv)
                    # Let's make sure to include an FVs which the ODFV requires Features from.
                    for projection in odfv.source_feature_view_projections.values():
                        fv = fvs[projection.name].with_projection(copy.copy(projection))
                        if fv not in fvs_to_use:
                            fvs_to_use.append(fv)
                else:
                    raise ValueError(
                        f"The provided feature service {features.name} contains a reference to a feature view"
                        f"{fv_name} which doesn't exist. Please make sure that you have created the feature view"
                        f'{fv_name} and that you have registered it by running "apply".'
                    )
            views_to_use = (fvs_to_use, request_fvs_to_use, od_fvs_to_use)
        else:
            views_to_use = (
                [*fvs.values()],
                [*request_fvs.values()],
                [*od_fvs.values()],
            )

        return views_to_use

    @log_exceptions_and_usage
    def serve(
        self,
        host: str,
        port: int,
        type_: str,
        no_access_log: bool,
        no_feature_log: bool,
    ) -> None:
        """Start the feature consumption server locally on a given port."""
        type_ = type_.lower()
        if self.config.go_feature_serving:
            # Start go server instead of python if the flag is enabled
            self._lazy_init_go_server()
            enable_logging = (
                self.config.feature_server
                and self.config.feature_server.feature_logging
                and self.config.feature_server.feature_logging.enabled
                and not no_feature_log
            )
            logging_options = (
                self.config.feature_server.feature_logging
                if enable_logging and self.config.feature_server
                else None
            )
            if type_ == "http":
                self._go_server.start_http_server(
                    host,
                    port,
                    enable_logging=enable_logging,
                    logging_options=logging_options,
                )
            elif type_ == "grpc":
                self._go_server.start_grpc_server(
                    host,
                    port,
                    enable_logging=enable_logging,
                    logging_options=logging_options,
                )
            else:
                raise ValueError(
                    f"Unsupported server type '{type_}'. Must be one of 'http' or 'grpc'."
                )
        else:
            if type_ != "http":
                raise ValueError(
                    f"Python server only supports 'http'. Got '{type_}' instead."
                )
            # Start the python server if go server isn't enabled
            feature_server.start_server(self, host, port, no_access_log)

    @log_exceptions_and_usage
    def get_feature_server_endpoint(self) -> Optional[str]:
        """Returns endpoint for the feature server, if it exists."""
        return self._provider.get_feature_server_endpoint()

    @log_exceptions_and_usage
    def serve_ui(
        self, host: str, port: int, get_registry_dump: Callable, registry_ttl_sec: int
    ) -> None:
        """Start the UI server locally"""
        warnings.warn(
            "The Feast UI is an experimental feature. "
            "We do not guarantee that future changes will maintain backward compatibility.",
            RuntimeWarning,
        )
        ui_server.start_server(
            self,
            host=host,
            port=port,
            get_registry_dump=get_registry_dump,
            project_id=self.config.project,
            registry_ttl_sec=registry_ttl_sec,
        )

    @log_exceptions_and_usage
    def serve_transformations(self, port: int) -> None:
        """Start the feature transformation server locally on a given port."""
        warnings.warn(
            "On demand feature view is an experimental feature. "
            "This API is stable, but the functionality does not scale well for offline retrieval",
            RuntimeWarning,
        )

        from feast import transformation_server

        transformation_server.start_server(self, port)

    def _teardown_go_server(self):
        self._go_server = None

    @log_exceptions_and_usage
    def write_logged_features(
        self, logs: Union[pa.Table, Path], source: FeatureService
    ):
        """
        Write logs produced by a source (currently only feature service is supported as a source)
        to an offline store.

        Args:
            logs: Arrow Table or path to parquet dataset directory on disk
            source: Object that produces logs
        """
        if not isinstance(source, FeatureService):
            raise ValueError("Only feature service is currently supported as a source")

        assert (
            source.logging_config is not None
        ), "Feature service must be configured with logging config in order to use this functionality"

        assert isinstance(logs, (pa.Table, Path))

        self._get_provider().write_feature_service_logs(
            feature_service=source,
            logs=logs,
            config=self.config,
            registry=self._registry,
        )

    @log_exceptions_and_usage
    def validate_logged_features(
        self,
        source: FeatureService,
        start: datetime,
        end: datetime,
        reference: ValidationReference,
        throw_exception: bool = True,
        cache_profile: bool = True,
    ) -> Optional[ValidationFailed]:
        """
        Load logged features from an offline store and validate them against provided validation reference.

        Args:
            source: Logs source object (currently only feature services are supported)
            start: lower bound for loading logged features
            end:  upper bound for loading logged features
            reference: validation reference
            throw_exception: throw exception or return it as a result
            cache_profile: store cached profile in Feast registry

        Returns:
            Throw or return (depends on parameter) ValidationFailed exception if validation was not successful
            or None if successful.

        """
        warnings.warn(
            "Logged features validation is an experimental feature. "
            "This API is unstable and it could and most probably will be changed in the future. "
            "We do not guarantee that future changes will maintain backward compatibility.",
            RuntimeWarning,
        )

        if not isinstance(source, FeatureService):
            raise ValueError("Only feature service is currently supported as a source")

        j = self._get_provider().retrieve_feature_service_logs(
            feature_service=source,
            start_date=start,
            end_date=end,
            config=self.config,
            registry=self.registry,
        )

        # read and run validation
        try:
            t = j.to_arrow(validation_reference=reference)
        except ValidationFailed as exc:
            if throw_exception:
                raise

            return exc
        else:
            print(f"{t.shape[0]} rows were validated.")

        if cache_profile:
            self.apply(reference)

        return None

    @log_exceptions_and_usage
    def get_validation_reference(
        self, name: str, allow_cache: bool = False
    ) -> ValidationReference:
        """
        Retrieves a validation reference.

        Raises:
            ValidationReferenceNotFoundException: The validation reference could not be found.
        """
        ref = self._registry.get_validation_reference(
            name, project=self.project, allow_cache=allow_cache
        )
        ref._dataset = self.get_saved_dataset(ref.dataset_name)
        return ref


def _validate_entity_values(join_key_values: Dict[str, List[Value]]):
    set_of_row_lengths = {len(v) for v in join_key_values.values()}
    if len(set_of_row_lengths) > 1:
        raise ValueError("All entity rows must have the same columns.")
    return set_of_row_lengths.pop()


def _validate_feature_refs(feature_refs: List[str], full_feature_names: bool = False):
    """
    Validates that there are no collisions among the feature references.

    Args:
        feature_refs: List of feature references to validate. Feature references must have format
            "feature_view:feature", e.g. "customer_fv:daily_transactions".
        full_feature_names: If True, the full feature references are compared for collisions; if False,
            only the feature names are compared.

    Raises:
        FeatureNameCollisionError: There is a collision among the feature references.
    """
    collided_feature_refs = []

    if full_feature_names:
        collided_feature_refs = [
            ref for ref, occurrences in Counter(feature_refs).items() if occurrences > 1
        ]
    else:
        feature_names = [ref.split(":")[1] for ref in feature_refs]
        collided_feature_names = [
            ref
            for ref, occurrences in Counter(feature_names).items()
            if occurrences > 1
        ]

        for feature_name in collided_feature_names:
            collided_feature_refs.extend(
                [ref for ref in feature_refs if ref.endswith(":" + feature_name)]
            )

    if len(collided_feature_refs) > 0:
        raise FeatureNameCollisionError(collided_feature_refs, full_feature_names)


def _group_feature_refs(
    features: List[str],
    all_feature_views: List[FeatureView],
    all_request_feature_views: List[RequestFeatureView],
    all_on_demand_feature_views: List[OnDemandFeatureView],
) -> Tuple[
    List[Tuple[FeatureView, List[str]]],
    List[Tuple[OnDemandFeatureView, List[str]]],
    List[Tuple[RequestFeatureView, List[str]]],
    Set[str],
]:
    """Get list of feature views and corresponding feature names based on feature references"""

    # view name to view proto
    view_index = {view.projection.name_to_use(): view for view in all_feature_views}

    # request view name to proto
    request_view_index = {
        view.projection.name_to_use(): view for view in all_request_feature_views
    }

    # on demand view to on demand view proto
    on_demand_view_index = {
        view.projection.name_to_use(): view for view in all_on_demand_feature_views
    }

    # view name to feature names
    views_features = defaultdict(set)
    request_views_features = defaultdict(set)
    request_view_refs = set()

    # on demand view name to feature names
    on_demand_view_features = defaultdict(set)

    for ref in features:
        view_name, feat_name = ref.split(":")
        if view_name in view_index:
            view_index[view_name].projection.get_feature(feat_name)  # For validation
            views_features[view_name].add(feat_name)
        elif view_name in on_demand_view_index:
            on_demand_view_index[view_name].projection.get_feature(
                feat_name
            )  # For validation
            on_demand_view_features[view_name].add(feat_name)
            # Let's also add in any FV Feature dependencies here.
            for input_fv_projection in on_demand_view_index[
                view_name
            ].source_feature_view_projections.values():
                for input_feat in input_fv_projection.features:
                    views_features[input_fv_projection.name].add(input_feat.name)
        elif view_name in request_view_index:
            request_view_index[view_name].projection.get_feature(
                feat_name
            )  # For validation
            request_views_features[view_name].add(feat_name)
            request_view_refs.add(ref)
        else:
            raise FeatureViewNotFoundException(view_name)

    fvs_result: List[Tuple[FeatureView, List[str]]] = []
    odfvs_result: List[Tuple[OnDemandFeatureView, List[str]]] = []
    request_fvs_result: List[Tuple[RequestFeatureView, List[str]]] = []

    for view_name, feature_names in views_features.items():
        fvs_result.append((view_index[view_name], list(feature_names)))
    for view_name, feature_names in request_views_features.items():
        request_fvs_result.append((request_view_index[view_name], list(feature_names)))
    for view_name, feature_names in on_demand_view_features.items():
        odfvs_result.append((on_demand_view_index[view_name], list(feature_names)))
    return fvs_result, odfvs_result, request_fvs_result, request_view_refs


def _print_materialization_log(
    start_date, end_date, num_feature_views: int, online_store: str
):
    if start_date:
        print(
            f"Materializing {Style.BRIGHT + Fore.GREEN}{num_feature_views}{Style.RESET_ALL} feature views"
            f" from {Style.BRIGHT + Fore.GREEN}{start_date.replace(microsecond=0).astimezone()}{Style.RESET_ALL}"
            f" to {Style.BRIGHT + Fore.GREEN}{end_date.replace(microsecond=0).astimezone()}{Style.RESET_ALL}"
            f" into the {Style.BRIGHT + Fore.GREEN}{online_store}{Style.RESET_ALL} online store.\n"
        )
    else:
        print(
            f"Materializing {Style.BRIGHT + Fore.GREEN}{num_feature_views}{Style.RESET_ALL} feature views"
            f" to {Style.BRIGHT + Fore.GREEN}{end_date.replace(microsecond=0).astimezone()}{Style.RESET_ALL}"
            f" into the {Style.BRIGHT + Fore.GREEN}{online_store}{Style.RESET_ALL} online store.\n"
        )


def _validate_feature_views(feature_views: List[BaseFeatureView]):
    """Verify feature views have case-insensitively unique names"""
    fv_names = set()
    for fv in feature_views:
        case_insensitive_fv_name = fv.name.lower()
        if case_insensitive_fv_name in fv_names:
            raise ValueError(
                f"More than one feature view with name {case_insensitive_fv_name} found. "
                f"Please ensure that all feature view names are case-insensitively unique. "
                f"It may be necessary to ignore certain files in your feature repository by using a .feastignore file."
            )
        else:
            fv_names.add(case_insensitive_fv_name)


def _validate_data_sources(data_sources: List[DataSource]):
    """Verify data sources have case-insensitively unique names"""
    ds_names = set()
    for ds in data_sources:
        case_insensitive_ds_name = ds.name.lower()
        if case_insensitive_ds_name in ds_names:
            if case_insensitive_ds_name.strip():
                warnings.warn(
                    f"More than one data source with name {case_insensitive_ds_name} found. "
                    f"Please ensure that all data source names are case-insensitively unique. "
                    f"It may be necessary to ignore certain files in your feature repository by using a .feastignore "
                    f"file. Starting in Feast 0.24, unique names (perhaps inferred from the table name) will be "
                    f"required in data sources to encourage data source discovery"
                )
        else:
            ds_names.add(case_insensitive_ds_name)


def apply_list_mapping(
    lst: Iterable[Any], mapping_indexes: Iterable[List[int]]
) -> Iterable[Any]:
    output_len = sum(len(item) for item in mapping_indexes)
    output = [None] * output_len
    for elem, destinations in zip(lst, mapping_indexes):
        for idx in destinations:
            output[idx] = elem

    return output
