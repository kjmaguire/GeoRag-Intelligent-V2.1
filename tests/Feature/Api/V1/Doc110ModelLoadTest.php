<?php

namespace Tests\Feature\Api\V1;

use Tests\TestCase;

/**
 * Smoke test for doc-phase 110 — verifies the new Eloquent models +
 * factories autoload + class_exists cleanly.
 *
 * No DB I/O. Just class autoloading. Lets CI catch typos / namespace
 * drifts before any consumer references the classes.
 */
class Doc110ModelLoadTest extends TestCase
{
    public function test_target_recommendation_loads(): void
    {
        $this->assertTrue(class_exists(\App\Models\Targeting\TargetRecommendation::class));
    }

    public function test_target_review_decision_loads(): void
    {
        $this->assertTrue(class_exists(\App\Models\Targeting\TargetReviewDecision::class));
    }

    public function test_target_outcome_loads(): void
    {
        $this->assertTrue(class_exists(\App\Models\Targeting\TargetOutcome::class));
    }

    public function test_hypothesis_loads(): void
    {
        $this->assertTrue(class_exists(\App\Models\Silver\Hypothesis::class));
    }

    public function test_hypothesis_evidence_link_loads(): void
    {
        $this->assertTrue(class_exists(\App\Models\Silver\HypothesisEvidenceLink::class));
    }

    public function test_decision_record_loads(): void
    {
        $this->assertTrue(class_exists(\App\Models\Silver\DecisionRecord::class));
    }

    public function test_decision_evidence_link_loads(): void
    {
        $this->assertTrue(class_exists(\App\Models\Silver\DecisionEvidenceLink::class));
    }

    public function test_decision_option_loads(): void
    {
        $this->assertTrue(class_exists(\App\Models\Silver\DecisionOption::class));
    }

    public function test_decision_outcome_loads(): void
    {
        $this->assertTrue(class_exists(\App\Models\Silver\DecisionOutcome::class));
    }

    public function test_decision_lesson_learned_loads(): void
    {
        $this->assertTrue(class_exists(\App\Models\Silver\DecisionLessonLearned::class));
    }

    public function test_target_recommendation_factory_loads(): void
    {
        $this->assertTrue(class_exists(\Database\Factories\Targeting\TargetRecommendationFactory::class));
    }

    public function test_hypothesis_factory_loads(): void
    {
        $this->assertTrue(class_exists(\Database\Factories\Silver\HypothesisFactory::class));
    }

    public function test_decision_record_factory_loads(): void
    {
        $this->assertTrue(class_exists(\Database\Factories\Silver\DecisionRecordFactory::class));
    }
}
