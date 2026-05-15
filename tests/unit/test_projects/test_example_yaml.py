"""Validates the public project registry example."""

from pathlib import Path

from src.projects.registry import load_project_registry

APPROVED_DIRECTORY = Path(__file__).parents[3]
EXAMPLE_YAML = APPROVED_DIRECTORY / "config" / "projects.example.yaml"


def test_projects_example_yaml_loads_enabled_projects():
    registry = load_project_registry(EXAMPLE_YAML, APPROVED_DIRECTORY)
    enabled = registry.list_enabled()
    assert len(enabled) == 2
    slugs = {p.slug for p in enabled}
    assert slugs == {"telegram-claude-code-bridge", "docs"}


def test_projects_example_yaml_paths_resolve_inside_approved_directory():
    registry = load_project_registry(EXAMPLE_YAML, APPROVED_DIRECTORY)
    for project in registry.list_enabled():
        assert (
            APPROVED_DIRECTORY in project.absolute_path.parents
            or project.absolute_path == APPROVED_DIRECTORY
        )
