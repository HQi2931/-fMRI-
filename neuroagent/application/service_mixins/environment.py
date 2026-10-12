"""Environment and local workspace selection use cases."""

from __future__ import annotations

from neuroagent.application.contracts.environment import (
    EnvironmentConfigUpdate,
    EnvironmentConfigView,
    EnvironmentProbeView,
    HealthView,
)
from neuroagent.application.contracts.projects import WorkspacePickView
from neuroagent.application.ports import WorkspacePickerPort
from neuroagent.application.service_mixins._base import BaseServiceMixin


class EnvironmentMixin(BaseServiceMixin):
    workspace_picker: WorkspacePickerPort | None

    def health(self) -> HealthView:
        self.database.ping()
        return HealthView()

    def environment_probe(self) -> EnvironmentProbeView:
        return self.environment_provider.current().probe

    def environment_config(self) -> EnvironmentConfigView:
        return self.environment_provider.configuration_view()

    def update_environment_config(self, request: EnvironmentConfigUpdate) -> EnvironmentConfigView:
        return self.environment_provider.update_configuration(request)

    def pick_workspace(self) -> WorkspacePickView:
        if self.workspace_picker is None:
            return WorkspacePickView(cancelled=True)
        return WorkspacePickView(path=self.workspace_picker.pick_directory())
