"""
Unit tests for permissions/input_sanitizer.py

Tests the security validation functions that protect against:
- Path traversal attacks
- Invalid URL protocols
- Shell command injection
- Payment action interception
"""

import pytest
from pathlib import Path

from browser_agent_system_v5.permissions.input_sanitizer import (
    sanitize_path,
    sanitize_url,
    sanitize_shell_input,
    sanitize_payment_action,
    SanitizationError
)


class TestSanitizePath:
    """Test path sanitization and traversal prevention"""
    
    def test_sanitize_path_with_valid_path(self, temp_worktree):
        """Test that valid relative paths are properly resolved"""
        path = "data/file.txt"
        result = sanitize_path(path, str(temp_worktree))
        
        assert str(temp_worktree) in result
        # Check that the resolved path ends with the expected components (OS-agnostic)
        result_path = Path(result)
        assert result_path.is_absolute()
        assert result_path.name == "file.txt"
        assert result_path.parent.name == "data"
    
    def test_sanitize_path_blocks_traversal_attack(self, temp_worktree):
        """Test that path traversal attacks are blocked"""
        malicious_paths = [
            "../../../../etc/passwd",
            "../../../etc/shadow",
            "../../.ssh/id_rsa",
            "data/../../../../../../etc/hosts"
        ]
        
        for path in malicious_paths:
            with pytest.raises(SanitizationError) as exc_info:
                sanitize_path(path, str(temp_worktree))
            assert "路径穿越攻击" in str(exc_info.value)
    
    def test_sanitize_path_with_missing_worktree(self):
        """Test that missing worktree root raises error"""
        with pytest.raises(SanitizationError) as exc_info:
            sanitize_path("file.txt", "")
        assert "未检测到 WorkTree 根目录" in str(exc_info.value)
    
    def test_sanitize_path_with_absolute_path_inside_worktree(self, temp_worktree):
        """Test that absolute paths inside worktree are allowed"""
        safe_path = temp_worktree / "data" / "file.txt"
        result = sanitize_path(str(safe_path), str(temp_worktree))
        assert str(temp_worktree) in result
    
    def test_sanitize_path_with_symlink_escape_attempt(self, temp_worktree):
        """Test that symlink-based escapes are blocked"""
        # This would require actual symlink creation, simplified test
        path = "data/../../../etc/passwd"
        with pytest.raises(SanitizationError):
            sanitize_path(path, str(temp_worktree))


class TestSanitizeUrl:
    """Test URL protocol validation"""
    
    def test_sanitize_url_with_valid_https(self):
        """Test that HTTPS URLs are allowed"""
        url = "https://example.com/page"
        result = sanitize_url(url)
        assert result == url
    
    def test_sanitize_url_with_valid_http(self):
        """Test that HTTP URLs are allowed"""
        url = "http://example.com/page"
        result = sanitize_url(url)
        assert result == url
    
    def test_sanitize_url_blocks_file_protocol(self):
        """Test that file:// protocol is blocked"""
        urls = [
            "file:///etc/passwd",
            "file://C:/Windows/System32/config/sam",
            "file:///home/user/.ssh/id_rsa"
        ]
        
        for url in urls:
            with pytest.raises(SanitizationError) as exc_info:
                sanitize_url(url)
            assert "禁止的 URL 协议" in str(exc_info.value)
    
    def test_sanitize_url_blocks_javascript_protocol(self):
        """Test that javascript: protocol is blocked"""
        urls = [
            "javascript:alert(1)",
            "javascript:void(0)",
            "javascript:document.cookie"
        ]
        
        for url in urls:
            with pytest.raises(SanitizationError) as exc_info:
                sanitize_url(url)
            assert "禁止的 URL 协议" in str(exc_info.value)
    
    def test_sanitize_url_blocks_data_protocol(self):
        """Test that data: protocol is blocked"""
        url = "data:text/html,<script>alert(1)</script>"
        with pytest.raises(SanitizationError) as exc_info:
            sanitize_url(url)
        assert "禁止的 URL 协议" in str(exc_info.value)
    
    def test_sanitize_url_with_missing_domain(self):
        """Test that URLs without domain are blocked"""
        with pytest.raises(SanitizationError) as exc_info:
            sanitize_url("http://")
        assert "缺少有效域名" in str(exc_info.value)


class TestSanitizeShellInput:
    """Test shell command injection prevention"""
    
    def test_sanitize_shell_input_with_safe_input(self):
        """Test that safe input passes validation"""
        safe_inputs = [
            "script.py",
            "data_file.txt",
            "my-script-name.py",
            "file_123.txt"
        ]
        
        for input_str in safe_inputs:
            result = sanitize_shell_input(input_str)
            assert result == input_str
    
    def test_sanitize_shell_input_blocks_pipe_character(self):
        """Test that pipe character is blocked"""
        with pytest.raises(SanitizationError) as exc_info:
            sanitize_shell_input("script.py | cat /etc/passwd")
        assert "危险字符" in str(exc_info.value)
    
    def test_sanitize_shell_input_blocks_redirect(self):
        """Test that redirect operators are blocked"""
        dangerous_inputs = [
            "script.py > /etc/passwd",
            "script.py >> /var/log/system.log",
            "script.py < /etc/shadow"
        ]
        
        for input_str in dangerous_inputs:
            with pytest.raises(SanitizationError):
                sanitize_shell_input(input_str)
    
    def test_sanitize_shell_input_blocks_command_chaining(self):
        """Test that command chaining is blocked"""
        dangerous_inputs = [
            "script.py; rm -rf /",
            "script.py && cat /etc/passwd",
            "script.py & background_task"
        ]
        
        for input_str in dangerous_inputs:
            with pytest.raises(SanitizationError):
                sanitize_shell_input(input_str)
    
    def test_sanitize_shell_input_blocks_backticks(self):
        """Test that backticks (command substitution) are blocked"""
        with pytest.raises(SanitizationError):
            sanitize_shell_input("script.py `cat /etc/passwd`")
    
    def test_sanitize_shell_input_blocks_dollar_expansion(self):
        """Test that $() command substitution is blocked"""
        with pytest.raises(SanitizationError):
            sanitize_shell_input("script.py $(cat /etc/passwd)")
    
    def test_sanitize_shell_input_blocks_rm_command(self):
        """Test that rm commands are blocked"""
        dangerous_inputs = [
            "rm -rf /",
            "rm --recursive --force /home",
            "rm -r important_data"
        ]
        
        for input_str in dangerous_inputs:
            with pytest.raises(SanitizationError):
                sanitize_shell_input(input_str)


class TestSanitizePaymentAction:
    """Test payment action interception"""
    
    def test_sanitize_payment_action_blocks_payment_click(self):
        """Test that clicking payment buttons is blocked"""
        payment_selectors = [
            "#pay-button",
            ".checkout-btn",
            "button[name='purchase']",
            ".buy-now",
            "#submit-payment",
            ".confirm-order"
        ]
        
        for selector in payment_selectors:
            with pytest.raises(SanitizationError) as exc_info:
                sanitize_payment_action("click_element", {"selector": selector})
            assert "支付安全拦截" in str(exc_info.value)
    
    def test_sanitize_payment_action_blocks_chinese_payment_keywords(self):
        """Test that Chinese payment keywords are blocked"""
        chinese_selectors = [
            "#支付按钮",
            ".立即购买",
            "button[name='确认订单']",
            ".结算"
        ]
        
        for selector in chinese_selectors:
            with pytest.raises(SanitizationError):
                sanitize_payment_action("click_element", {"selector": selector})
    
    def test_sanitize_payment_action_blocks_card_input(self):
        """Test that credit card input is blocked"""
        with pytest.raises(SanitizationError) as exc_info:
            sanitize_payment_action("fill_form", {
                "selector": "#card-number",
                "value": "4111111111111111"
            })
        assert "支付安全拦截" in str(exc_info.value)
    
    def test_sanitize_payment_action_blocks_card_number_pattern(self):
        """Test that card number patterns are detected"""
        card_numbers = [
            "4111111111111111",  # Visa
            "5500 0000 0000 0004",  # Mastercard with spaces
            "3400-0000-0000-009",  # Amex with dashes
        ]
        
        for card_num in card_numbers:
            with pytest.raises(SanitizationError) as exc_info:
                sanitize_payment_action("fill_form", {
                    "selector": "#any-input",
                    "value": card_num
                })
            assert "银行卡号" in str(exc_info.value)
    
    def test_sanitize_payment_action_blocks_password_fields(self):
        """Test that password field filling is blocked"""
        password_selectors = [
            "#password",
            "input[type='password']",
            "#payment-password",
            ".security-code"
        ]
        
        for selector in password_selectors:
            with pytest.raises(SanitizationError):
                sanitize_payment_action("fill_form", {
                    "selector": selector,
                    "value": "test123"
                })
    
    def test_sanitize_payment_action_allows_safe_click(self):
        """Test that safe clicks are allowed"""
        safe_selectors = [
            "#search-button",
            ".nav-link",
            "button[name='submit-form']",
            ".close-modal"
        ]
        
        for selector in safe_selectors:
            # Should not raise exception
            sanitize_payment_action("click_element", {"selector": selector})
    
    def test_sanitize_payment_action_allows_safe_input(self):
        """Test that safe input is allowed"""
        # Should not raise exception
        sanitize_payment_action("fill_form", {
            "selector": "#username",
            "value": "testuser"
        })
        
        sanitize_payment_action("fill_form", {
            "selector": "#email",
            "value": "test@example.com"
        })


class TestEdgeCases:
    """Test edge cases and boundary conditions"""
    
    def test_sanitize_path_with_unicode_characters(self, temp_worktree):
        """Test path with unicode characters"""
        path = "数据/文件.txt"
        result = sanitize_path(path, str(temp_worktree))
        assert str(temp_worktree) in result
    
    def test_sanitize_url_with_unicode_domain(self):
        """Test URL with unicode domain (IDN)"""
        url = "https://例え.jp/page"
        result = sanitize_url(url)
        assert result == url
    
    def test_sanitize_shell_input_with_spaces(self):
        """Test shell input with spaces"""
        input_str = "my script with spaces.py"
        result = sanitize_shell_input(input_str)
        assert result == input_str
    
    def test_sanitize_payment_action_case_insensitive(self):
        """Test that payment detection is case-insensitive"""
        with pytest.raises(SanitizationError):
            sanitize_payment_action("click_element", {"selector": "#PAY-BUTTON"})
        
        with pytest.raises(SanitizationError):
            sanitize_payment_action("click_element", {"selector": "#Pay-Button"})
