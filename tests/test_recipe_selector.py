"""Tests for execution intent and recipe selection."""

from src.product.services.recipe_selector import RecipeSelector


def test_selects_brownfield_recipe_for_small_linked_request():
    selector = RecipeSelector()
    request = {
        "id": "req-1",
        "title": "Tweak hero",
        "description": "Show request count in the hero.",
        "linked_paths": ["src/product/static/app.js", "src/product/static/index.html"],
    }
    context = {
        "linked_paths": ["src/product/static/app.js", "src/product/static/index.html"],
    }

    intent, recipe = selector.select_for_request(request, context_bundle=context)

    assert intent["intent_type"] == "brownfield_change"
    assert intent["scope_level"] == "tiny"
    assert recipe["recipe_id"] == "brownfield-scoped-v1"
    assert recipe["planning_policy"] == "minimal"


def test_selects_greenfield_recipe_when_spec_is_present():
    selector = RecipeSelector()
    request = {
        "id": "req-1",
        "title": "Build app",
        "description": "Build a new app from a spec.",
        "linked_paths": ["docs/spec.md"],
    }
    context = {"linked_paths": ["docs/spec.md"]}

    intent, recipe = selector.select_for_request(request, context_bundle=context)

    assert intent["intent_type"] == "greenfield_build"
    assert recipe["recipe_id"] == "greenfield-full-v1"
