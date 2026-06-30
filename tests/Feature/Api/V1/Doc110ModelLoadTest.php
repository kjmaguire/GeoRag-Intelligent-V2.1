<?php

namespace Tests\Feature\Api\V1;

use App\Models\Silver\DecisionEvidenceLink;
use App\Models\Silver\DecisionLessonLearned;
use App\Models\Silver\DecisionOption;
use App\Models\Silver\DecisionOutcome;
use App\Models\Silver\DecisionRecord;
use App\Models\Silver\Hypothesis;
use App\Models\Silver\HypothesisEvidenceLink;
use App\Models\Targeting\TargetOutcome;
use App\Models\Targeting\TargetRecommendation;
use App\Models\Targeting\TargetReviewDecision;
use Database\Factories\Silver\DecisionRecordFactory;
use Database\Factories\Silver\HypothesisFactory;
use Database\Factories\Targeting\TargetRecommendationFactory;
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
        $this->assertTrue(class_exists(TargetRecommendation::class));
    }

    public function test_target_review_decision_loads(): void
    {
        $this->assertTrue(class_exists(TargetReviewDecision::class));
    }

    public function test_target_outcome_loads(): void
    {
        $this->assertTrue(class_exists(TargetOutcome::class));
    }

    public function test_hypothesis_loads(): void
    {
        $this->assertTrue(class_exists(Hypothesis::class));
    }

    public function test_hypothesis_evidence_link_loads(): void
    {
        $this->assertTrue(class_exists(HypothesisEvidenceLink::class));
    }

    public function test_decision_record_loads(): void
    {
        $this->assertTrue(class_exists(DecisionRecord::class));
    }

    public function test_decision_evidence_link_loads(): void
    {
        $this->assertTrue(class_exists(DecisionEvidenceLink::class));
    }

    public function test_decision_option_loads(): void
    {
        $this->assertTrue(class_exists(DecisionOption::class));
    }

    public function test_decision_outcome_loads(): void
    {
        $this->assertTrue(class_exists(DecisionOutcome::class));
    }

    public function test_decision_lesson_learned_loads(): void
    {
        $this->assertTrue(class_exists(DecisionLessonLearned::class));
    }

    public function test_target_recommendation_factory_loads(): void
    {
        $this->assertTrue(class_exists(TargetRecommendationFactory::class));
    }

    public function test_hypothesis_factory_loads(): void
    {
        $this->assertTrue(class_exists(HypothesisFactory::class));
    }

    public function test_decision_record_factory_loads(): void
    {
        $this->assertTrue(class_exists(DecisionRecordFactory::class));
    }
}
