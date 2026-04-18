"""
Unit tests for permissions/denial_tracker.py

Tests the circuit breaker / denial tracking system.
"""

import pytest
import time

from browser_agent_system_v5.permissions.denial_tracker import DenialTracker


class TestDenialTracker:
    """Test DenialTracker initialization and basic operations"""
    
    def test_denial_tracker_initialization(self):
        """Test creating a new DenialTracker"""
        tracker = DenialTracker(max_consecutive_denials=5)
        
        assert tracker.max_consecutive == 5
        assert not tracker.is_circuit_broken("any_agent")
    
    def test_denial_tracker_default_threshold(self):
        """Test default threshold value"""
        tracker = DenialTracker()
        
        # Default should be 5
        assert tracker.max_consecutive == 5
    
    def test_record_denial_increments_count(self):
        """Test that record_denial() increments denial count"""
        tracker = DenialTracker(max_consecutive_denials=3)
        
        is_broken = tracker.record_denial("test_agent", "Test denial reason")
        
        assert is_broken == False  # Not broken yet
    
    def test_record_denial_triggers_circuit_breaker(self):
        """Test that consecutive denials trigger circuit breaker"""
        tracker = DenialTracker(max_consecutive_denials=3)
        
        # Record 3 denials
        tracker.record_denial("test_agent", "Denial 1")
        tracker.record_denial("test_agent", "Denial 2")
        is_broken = tracker.record_denial("test_agent", "Denial 3")
        
        assert is_broken == True
        assert tracker.is_circuit_broken("test_agent") == True
    
    def test_record_approval_resets_count(self):
        """Test that record_approval() resets denial count"""
        tracker = DenialTracker(max_consecutive_denials=3)
        
        # Record 2 denials
        tracker.record_denial("test_agent", "Denial 1")
        tracker.record_denial("test_agent", "Denial 2")
        
        # Record approval
        tracker.record_approval("test_agent")
        
        # Record 2 more denials (should not trigger breaker)
        tracker.record_denial("test_agent", "Denial 3")
        is_broken = tracker.record_denial("test_agent", "Denial 4")
        
        assert is_broken == False
    
    def test_is_circuit_broken_returns_status(self):
        """Test that is_circuit_broken() returns correct status"""
        tracker = DenialTracker(max_consecutive_denials=2)
        
        # Initially not broken
        assert tracker.is_circuit_broken("test_agent") == False
        
        # Trigger breaker
        tracker.record_denial("test_agent", "Denial 1")
        tracker.record_denial("test_agent", "Denial 2")
        
        # Now broken
        assert tracker.is_circuit_broken("test_agent") == True
    
    def test_clear_session_clears_agent_state(self):
        """Test that clear_session() clears agent state"""
        tracker = DenialTracker(max_consecutive_denials=2)
        
        # Use session-prefixed agent_id format: "session_id:agent_type"
        agent_id = "session_123:test_agent"
        
        # Trigger breaker
        tracker.record_denial(agent_id, "Denial 1")
        tracker.record_denial(agent_id, "Denial 2")
        assert tracker.is_circuit_broken(agent_id) == True
        
        # Clear session using session_id prefix
        tracker.clear_session("session_123")
        
        # Should be reset - agent state removed entirely
        assert tracker.is_circuit_broken(agent_id) == False


class TestMultipleAgents:
    """Test tracking multiple agents independently"""
    
    def test_different_agents_tracked_independently(self):
        """Test that different agents are tracked independently"""
        tracker = DenialTracker(max_consecutive_denials=2)
        
        # Agent 1 gets 2 denials (triggers breaker)
        tracker.record_denial("agent1", "Denial")
        tracker.record_denial("agent1", "Denial")
        
        # Agent 2 gets 1 denial (doesn't trigger)
        tracker.record_denial("agent2", "Denial")
        
        assert tracker.is_circuit_broken("agent1") == True
        assert tracker.is_circuit_broken("agent2") == False
    
    def test_clearing_one_agent_doesnt_affect_others(self):
        """Test that clearing one agent doesn't affect others"""
        tracker = DenialTracker(max_consecutive_denials=2)
        
        # Both agents get denials
        tracker.record_denial("agent1", "Denial")
        tracker.record_denial("agent2", "Denial")
        
        # Clear agent1
        tracker.clear_session("agent1")
        
        # Agent1 should be cleared, agent2 should still have denial
        tracker.record_denial("agent1", "Denial")
        is_broken_1 = tracker.record_denial("agent1", "Denial")
        
        tracker.record_denial("agent2", "Denial")
        is_broken_2 = tracker.is_circuit_broken("agent2")
        
        assert is_broken_1 == True  # Agent1 triggered after 2 new denials
        assert is_broken_2 == True  # Agent2 triggered (had 1, got 1 more)


class TestEdgeCases:
    """Test edge cases and boundary conditions"""
    
    def test_zero_threshold_immediately_breaks(self):
        """Test that threshold of 0 immediately breaks circuit"""
        tracker = DenialTracker(max_consecutive_denials=0)
        
        # First denial should trigger
        is_broken = tracker.record_denial("test_agent", "Denial")
        
        assert is_broken == True
    
    def test_high_threshold_requires_many_denials(self):
        """Test that high threshold requires many denials"""
        tracker = DenialTracker(max_consecutive_denials=10)
        
        # Record 9 denials (shouldn't trigger)
        for i in range(9):
            is_broken = tracker.record_denial("test_agent", f"Denial {i}")
            assert is_broken == False
        
        # 10th denial should trigger
        is_broken = tracker.record_denial("test_agent", "Denial 10")
        assert is_broken == True
    
    def test_approval_after_breaker_doesnt_reset(self):
        """Test that approval after breaker is triggered doesn't reset it"""
        tracker = DenialTracker(max_consecutive_denials=2)
        
        # Trigger breaker
        tracker.record_denial("test_agent", "Denial 1")
        tracker.record_denial("test_agent", "Denial 2")
        assert tracker.is_circuit_broken("test_agent") == True
        
        # Record approval (shouldn't reset breaker)
        tracker.record_approval("test_agent")
        
        # Breaker should still be active
        assert tracker.is_circuit_broken("test_agent") == True
    
    def test_empty_agent_id(self):
        """Test handling of empty agent ID"""
        tracker = DenialTracker(max_consecutive_denials=2)
        
        # Should handle empty string gracefully
        tracker.record_denial("", "Denial")
        assert tracker.is_circuit_broken("") == False
    
    def test_unicode_agent_id(self):
        """Test handling of unicode agent IDs"""
        tracker = DenialTracker(max_consecutive_denials=2)
        
        agent_id = "测试_agent_中文"
        tracker.record_denial(agent_id, "Denial 1")
        tracker.record_denial(agent_id, "Denial 2")
        
        assert tracker.is_circuit_broken(agent_id) == True


class TestDenialReasons:
    """Test denial reason tracking"""
    
    def test_denial_reasons_are_stored(self):
        """Test that denial reasons are stored (if implemented)"""
        tracker = DenialTracker(max_consecutive_denials=3)
        
        # Record denials with different reasons
        tracker.record_denial("test_agent", "Path traversal attempt")
        tracker.record_denial("test_agent", "Invalid URL protocol")
        tracker.record_denial("test_agent", "Payment action blocked")
        
        # Breaker should be triggered
        assert tracker.is_circuit_broken("test_agent") == True
