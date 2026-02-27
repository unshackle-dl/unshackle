from __future__ import annotations

import logging
import re
from typing import Any

from unshackle.core.utilities import sanitize_filename

log = logging.getLogger(__name__)


class TemplateFormatter:
    """
    Template formatter for custom filename patterns.

    Supports variable substitution and conditional variables.
    Example: '{title}.{year}.{quality?}.{source}-{tag}'
    """

    def __init__(self, template: str):
        """Initialize the template formatter.

        Args:
            template: Template string with variables in {variable} format
        """
        self.template = template
        self.variables = self._extract_variables()

    def _extract_variables(self) -> list[str]:
        """Extract all variables from the template."""
        pattern = r"\{([^}]+)\}"
        matches = re.findall(pattern, self.template)
        return [match.strip() for match in matches]

    def format(self, context: dict[str, Any]) -> str:
        """Format the template with the provided context.

        Args:
            context: Dictionary containing variable values

        Returns:
            Formatted filename string

        Raises:
            ValueError: If required template variables are missing from context
        """
        is_valid, missing_vars = self.validate(context)
        if not is_valid:
            error_msg = f"Missing required template variables: {', '.join(missing_vars)}"
            log.error(error_msg)
            raise ValueError(error_msg)

        try:
            result = self.template

            for variable in self.variables:
                placeholder = "{" + variable + "}"
                is_conditional = variable.endswith("?")

                if is_conditional:
                    var_name = variable[:-1]
                    value = context.get(var_name, "")

                    if value:
                        safe_value = str(value).strip()
                        result = result.replace(placeholder, safe_value)
                    else:
                        # Remove the placeholder and consume the adjacent separator on one side
                        # e.g. "{disc?}-{track}" → "{track}" when disc is empty
                        # e.g. "{title}.{edition?}.{quality}" → "{title}.{quality}" when edition is empty
                        def _remove_conditional(m: re.Match) -> str:
                            s = m.group(0)
                            has_left = s[0] in ".- "
                            has_right = s[-1] in ".- "
                            if has_left and has_right:
                                return s[0]  # keep left separator
                            return ""

                        result = re.sub(
                            rf"[\.\s\-]?{re.escape(placeholder)}[\.\s\-]?",
                            _remove_conditional,
                            result,
                            count=1,
                        )
                else:
                    value = context.get(variable, "")
                    if value is None:
                        log.warning(f"Template variable '{variable}' is None, using empty string")
                        value = ""

                    safe_value = str(value).strip()
                    result = result.replace(placeholder, safe_value)

            # Clean up multiple consecutive dots/separators and other artifacts
            result = re.sub(r"\.{2,}", ".", result)  # Multiple dots -> single dot
            result = re.sub(r"\s{2,}", " ", result)  # Multiple spaces -> single space
            result = re.sub(r"-{2,}", "-", result)  # Multiple dashes -> single dash
            result = re.sub(r"^[\.\s\-]+|[\.\s\-]+$", "", result)  # Remove leading/trailing dots, spaces, dashes
            result = re.sub(r"\.-", "-", result)  # Remove dots before dashes (for dot-based templates)
            result = re.sub(r"[\.\s]+\)", ")", result)  # Remove dots/spaces before closing parentheses
            result = re.sub(r"\(\s*\)", "", result)  # Remove empty parentheses (empty conditional)

            # Determine the appropriate separator based on template style
            # Count separator characters between variables (between } and {)
            between_vars = re.findall(r"\}([^{]*)\{", self.template)
            separator_text = "".join(between_vars)
            dot_count = separator_text.count(".")
            space_count = separator_text.count(" ")

            if space_count > dot_count:
                result = sanitize_filename(result, spacer=" ")
            else:
                result = sanitize_filename(result, spacer=".")

            if not result or result.isspace():
                log.warning("Template formatting resulted in empty filename, using fallback")
                return "untitled"

            log.debug(f"Template formatted successfully: '{self.template}' -> '{result}'")
            return result

        except (KeyError, ValueError, re.error) as e:
            log.error(f"Error formatting template '{self.template}': {e}")
            fallback = f"error_formatting_{hash(self.template) % 10000}"
            log.warning(f"Using fallback filename: {fallback}")
            return fallback

    def validate(self, context: dict[str, Any]) -> tuple[bool, list[str]]:
        """Validate that all required variables are present in context.

        Args:
            context: Dictionary containing variable values

        Returns:
            Tuple of (is_valid, missing_variables)
        """
        missing = []

        for variable in self.variables:
            is_conditional = variable.endswith("?")
            var_name = variable[:-1] if is_conditional else variable

            if not is_conditional and var_name not in context:
                missing.append(var_name)

        return len(missing) == 0, missing

    def get_required_variables(self) -> list[str]:
        """Get list of required (non-conditional) variables."""
        required = []
        for variable in self.variables:
            if not variable.endswith("?"):
                required.append(variable)
        return required

    def get_optional_variables(self) -> list[str]:
        """Get list of optional (conditional) variables."""
        optional = []
        for variable in self.variables:
            if variable.endswith("?"):
                optional.append(variable[:-1])  # Remove the ?
        return optional
