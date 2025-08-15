import re
from typing import Dict, Any, List, Optional

from unshackle.core.utilities import sanitize_filename


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
    
    def _extract_variables(self) -> List[str]:
        """Extract all variables from the template."""
        pattern = r'\{([^}]+)\}'
        matches = re.findall(pattern, self.template)
        return [match.strip() for match in matches]
    
    def format(self, context: Dict[str, Any]) -> str:
        """Format the template with the provided context.
        
        Args:
            context: Dictionary containing variable values
            
        Returns:
            Formatted filename string
        """
        result = self.template
        
        for variable in self.variables:
            placeholder = '{' + variable + '}'
            is_conditional = variable.endswith('?')
            
            if is_conditional:
                # Remove the ? for conditional variables
                var_name = variable[:-1]
                value = context.get(var_name, '')
                
                if value:
                    # Replace with actual value
                    result = result.replace(placeholder, str(value))
                else:
                    # Remove the placeholder entirely for empty conditional variables
                    result = result.replace(placeholder, '')
            else:
                # Regular variable
                value = context.get(variable, '')
                result = result.replace(placeholder, str(value))
        
        # Clean up multiple consecutive dots/separators and other artifacts
        result = re.sub(r'\.{2,}', '.', result)  # Multiple dots -> single dot
        result = re.sub(r'\s{2,}', ' ', result)  # Multiple spaces -> single space
        result = re.sub(r'^[\.\s]+|[\.\s]+$', '', result)  # Remove leading/trailing dots and spaces
        result = re.sub(r'\.-', '-', result)  # Remove dots before dashes (for dot-based templates)
        result = re.sub(r'[\.\s]+\)', ')', result)  # Remove dots/spaces before closing parentheses
        
        # Determine the appropriate separator based on template style
        # If the template contains spaces (like Plex-friendly), preserve them
        if ' ' in self.template and '.' not in self.template:
            # Space-based template (Plex-friendly) - use space separator
            result = sanitize_filename(result, spacer=' ')
        else:
            # Dot-based template (scene-style) - use dot separator
            result = sanitize_filename(result, spacer='.')
        
        return result
    
    def validate(self, context: Dict[str, Any]) -> tuple[bool, List[str]]:
        """Validate that all required variables are present in context.
        
        Args:
            context: Dictionary containing variable values
            
        Returns:
            Tuple of (is_valid, missing_variables)
        """
        missing = []
        
        for variable in self.variables:
            is_conditional = variable.endswith('?')
            var_name = variable[:-1] if is_conditional else variable
            
            # Only check non-conditional variables
            if not is_conditional and var_name not in context:
                missing.append(var_name)
        
        return len(missing) == 0, missing
    
    def get_required_variables(self) -> List[str]:
        """Get list of required (non-conditional) variables."""
        required = []
        for variable in self.variables:
            if not variable.endswith('?'):
                required.append(variable)
        return required
    
    def get_optional_variables(self) -> List[str]:
        """Get list of optional (conditional) variables."""
        optional = []
        for variable in self.variables:
            if variable.endswith('?'):
                optional.append(variable[:-1])  # Remove the ?
        return optional