from quality_scenarios import budget_scenario, catalog_scenario, curation_scenario, organization_scenario


def test_held_out_assignment_spans_targets_and_abstentions(tmp_path):
    metrics = organization_scenario(tmp_path / "organization")
    assert metrics["wrong_target_suggestions"] == metrics["suggestions_on_abstentions"] == 0, metrics
    assert metrics["fully_deferred_abstentions"] == 5, metrics
    assert not metrics["limit_reasons"], metrics
    cases = metrics["cases"]
    assert all(case["visible_characters"] == case["truth_characters"] for case in cases.values()), metrics
    for label in ("first_edition", "mixed_other_work", "revised_edition", "mixed_third_work", "clipped_copy"):
        assert cases[label]["exact_suggestion"], metrics
    for label in ("merged_boundary_left", "merged_boundary_right"):
        case = cases[label]
        assert case["exact_suggestion"] or case["characters_with_correct_manual_target"] == case["truth_characters"]
    assert cases["cross_book_common_text"]["alternative_targets"] == 2
    assert cases["same_book_common_editions"]["alternative_targets"] == 2
    assert cases["marked_quotation"]["alternative_targets"] == 1
    assert cases["quotation_with_introduction"]["alternative_targets"] == 1
    assert cases["unseen_material"]["alternative_targets"] == 0


def test_held_out_curation_retains_repeats_and_exact_provenance(tmp_path):
    metrics = curation_scenario(tmp_path / "curation")
    assert metrics["confirmed_conflict_cases"] == ["merge", "number", "ocr", "split"]
    assert metrics["byte_verified_output_ranges"] > 20


def test_old_catalog_records_remain_searchable_and_edition_scoped(tmp_path):
    metrics = catalog_scenario(tmp_path / "catalog")
    assert metrics["sources"] > 1_000 and metrics["books"] > 1_000


def test_long_multisource_plans_report_limits_and_allow_narrowing(tmp_path):
    metrics = budget_scenario(tmp_path / "budget")
    assert metrics["characters_per_long_block"] > 100_000
