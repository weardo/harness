"""Tests for the local-first request service and context assembly."""

import tempfile
from pathlib import Path

from src.product.services.context_assembler import ContextAssembler
from src.product.services.request_service import RequestService
from src.product.services.run_service import RunService


def test_request_service_create_and_get():
    with tempfile.TemporaryDirectory() as d:
        project_dir = Path(d)
        service = RequestService(project_dir)
        record = service.create_request(
            project_id="demo",
            title="Build auth",
            description="Add auth flow\nSupport sessions",
            acceptance_criteria=["Login works"],
        )
        fetched = service.get_request(record["id"])
        assert fetched is not None
        assert fetched["title"] == "Build auth"
        assert fetched["acceptance_criteria"] == ["Login works"]


def test_context_assembler_surfaces_linked_paths():
    with tempfile.TemporaryDirectory() as d:
        project_dir = Path(d)
        spec_path = project_dir / "docs" / "spec.md"
        spec_path.parent.mkdir(parents=True)
        spec_path.write_text("# Spec")

        assembler = ContextAssembler()
        request = {
            "id": "req-1",
            "project_id": "demo",
            "title": "Build auth",
            "description": "Add auth flow\nSupport sessions",
            "source": "manual",
            "linked_paths": ["docs/spec.md"],
            "acceptance_criteria": ["Login works"],
        }
        bundle = assembler.assemble(request, project_dir)
        assert bundle["request_summary"] == "Add auth flow"
        assert bundle["linked_paths"] == ["docs/spec.md"]
        assert bundle["linked_artifacts"][0]["name"] == "spec.md"


def test_run_service_builds_execution_request_from_request():
    with tempfile.TemporaryDirectory() as d:
        project_dir = Path(d)
        spec_path = project_dir / "docs" / "spec.md"
        spec_path.parent.mkdir(parents=True)
        spec_path.write_text("# Spec")

        run_service = RunService()
        request = {
            "id": "req-1",
            "project_id": "demo",
            "title": "Build auth",
            "description": "Add auth flow",
            "source": "manual",
            "linked_paths": ["docs/spec.md"],
            "acceptance_criteria": ["Login works"],
        }
        execution_request = run_service.build_execution_request_from_request(project_dir, request)
        assert execution_request["source_type"] == "request"
        assert execution_request["request_id"] == "req-1"
        assert execution_request["context_bundle"]["assembled_context"]["acceptance_criteria"] == ["Login works"]
        assert execution_request["context_bundle"]["spec_path"].endswith("docs/spec.md")
        assert execution_request["execution_recipe"]["recipe_id"] == "greenfield-full-v1"


def test_run_service_builds_brownfield_recipe_for_small_linked_request():
    with tempfile.TemporaryDirectory() as d:
        project_dir = Path(d)
        ui_path = project_dir / "src" / "ui.js"
        ui_path.parent.mkdir(parents=True)
        ui_path.write_text("console.log('ui');")

        run_service = RunService()
        request = {
            "id": "req-1",
            "project_id": "demo",
            "title": "Tweak hero",
            "description": "Show request count in the hero.",
            "source": "manual",
            "linked_paths": ["src/ui.js"],
            "acceptance_criteria": ["Hero shows request count"],
        }
        execution_request = run_service.build_execution_request_from_request(project_dir, request)
        assert execution_request["execution_intent"]["intent_type"] == "brownfield_change"
        assert execution_request["execution_recipe"]["recipe_id"] == "brownfield-scoped-v1"
