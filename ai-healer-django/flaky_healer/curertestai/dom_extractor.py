"""
DOM Extraction Utilities
Provides advanced methods to extract semantic and accessibility information from HTML
instead of sending the full DOM to the LLM.
"""

from bs4 import BeautifulSoup
from typing import List, Dict, Any, Optional
import json
import re


class DOMExtractor:
    """Extract semantic and accessibility information from HTML"""
    
    # Interactive element selectors (Default - Optimized for forms/controls)
    INTERACTIVE_SELECTORS = [
        'button', 'a', 'input', 'select', 'textarea',
        '[role="button"]', '[role="link"]', '[role="tab"]',
        '[role="menuitem"]', '[role="checkbox"]', '[role="radio"]',
        '[data-testid]', '[data-test]', '[data-cy]',
        '[aria-label]', '[onclick]'
    ]
    
    # Extended selectors for full page coverage (includes images, menus, banners)
    EXTENDED_INTERACTIVE_SELECTORS = [
        # Form elements
        'button', 'a', 'input', 'select', 'textarea', 'label',
        
        # Media elements
        'img', 'svg', 'video', 'audio', 'canvas',
        
        # List/Menu elements
        'li', 'ul', 'ol', 'nav',
        
        # Semantic sections
        'header', 'footer', 'main', 'aside', 'section', 'article',
        
        # Common clickable containers
        'div[onclick]', 'span[onclick]', 'div[class*="click"]', 
        'div[class*="button"]', 'div[class*="menu"]', 'div[class*="logo"]',
        'div[class*="banner"]', 'div[class*="card"]',
        
        # ARIA roles
        '[role="button"]', '[role="link"]', '[role="tab"]',
        '[role="menuitem"]', '[role="checkbox"]', '[role="radio"]',
        '[role="menu"]', '[role="navigation"]', '[role="banner"]',
        
        # Test IDs
        '[data-testid]', '[data-test]', '[data-cy]',
        
        # Interactive attributes
        '[aria-label]', '[onclick]', '[aria-expanded]', '[tabindex]'
    ]
    
    # Attributes to extract
    IMPORTANT_ATTRIBUTES = [
        'id', 'name', 'class', 'type', 'value', 'placeholder',
        'aria-label', 'aria-labelledby', 'aria-describedby',
        'role', 'title', 'alt', 'href', 'src',
        'data-testid', 'data-test', 'data-cy', 'data-action'
    ]
    
    def __init__(self, html: str):
        """Initialize with HTML content"""
        self.soup = BeautifulSoup(html, 'html.parser')
        self.html = html # Store original HTML for size calculation
        self.element_counter = 0
    
    def extract_semantic_dom(self, full_coverage: bool = False, max_elements: int = 500) -> Dict[str, Any]:
        """
        Extract semantic DOM focusing on interactive elements with optional full page coverage.
        For large DOMs, applies priority-based filtering to stay within max_elements limit.
        
        Args:
            full_coverage: If True, extract all interactive elements including images, menus, banners
            max_elements: Maximum number of elements to extract (default: 500)
        
        Returns:
            Dictionary with extracted elements and metadata
        """
        # Determine which selectors to use based on full_coverage
        selectors_to_use = self.EXTENDED_INTERACTIVE_SELECTORS if full_coverage else self.INTERACTIVE_SELECTORS
        
        # 1. First, extract using CSS selectors
        elements = []
        seen_elements = set()
        
        for selector in selectors_to_use:
            found_elements = self.soup.select(selector)
            for element in found_elements:
                elem_id = id(element)
                if elem_id not in seen_elements and self._is_visible(element):
                    elem_data = self._extract_element_data(element)
                    if elem_data:
                        elements.append(elem_data)
                        seen_elements.add(elem_id)
        
        # 2. Also scan all elements for interactive heuristics (ONLY if full_coverage=True)
        if full_coverage:
            for element in self.soup.find_all(True):
                elem_id = id(element)
                if elem_id not in seen_elements and self._is_likely_interactive(element) and self._is_visible(element):
                    elem_data = self._extract_element_data(element)
                    if elem_data:
                        elements.append(elem_data)
                        seen_elements.add(elem_id)
        
        # 3. Priority-based filtering if we exceed max_elements
        html_size = len(self.html)
        if len(elements) > max_elements or html_size > 500000:  # 500KB threshold
            elements = self._apply_priority_filtering(elements, max_elements)
        
        return {
            "type": "semantic_dom",
            "total_elements": len(elements),
            "full_coverage": full_coverage,
            "elements": elements,
            "metadata": {
                "extraction_method": "comprehensive_semantic" if full_coverage else "semantic_snapshot",
                "interactive_only": not full_coverage,
                "html_size_kb": html_size / 1024,
                "filtered": len(elements) >= max_elements
            }
        }
    
    def _apply_priority_filtering(self, elements: List[Dict[str, Any]], max_elements: int) -> List[Dict[str, Any]]:
        """
        Apply priority-based filtering to keep only the most relevant elements.
        
        Priority order:
        1. Elements with data-testid or data-test (test automation targets)
        2. Elements with aria-label (accessibility labeled)
        3. Elements with meaningful text content (> 3 chars)
        4. Interactive elements (buttons, links, inputs)
        5. Elements with id attributes
        """
        def calculate_priority(elem: Dict[str, Any]) -> int:
            attrs = elem.get('attributes', {})
            text = elem.get('text', '')
            tag = elem.get('tag', '')
            
            priority = 0
            
            # Highest priority: Test IDs
            if 'data-testid' in attrs or 'data-test' in attrs or 'data-cy' in attrs:
                priority += 100
            
            # High priority: Aria-label (semantic accessibility)
            if 'aria-label' in attrs and attrs['aria-label']:
                priority += 80
            
            # Medium-high priority: Meaningful text
            if text and len(text.strip()) > 3:
                priority += 60
                # Bonus for longer, descriptive text
                if len(text.strip()) > 10:
                    priority += 20
            
            # Medium priority: Interactive elements
            if tag in ['button', 'a', 'input', 'select', 'textarea']:
                priority += 50
            
            # Low-medium priority: ID attribute
            if 'id' in attrs:
                priority += 30
            
            # Low priority: Class attribute
            if 'class' in attrs:
                priority += 10
            
            return priority
        
        # Sort by priority (descending) and take top max_elements
        elements_with_priority = [(elem, calculate_priority(elem)) for elem in elements]
        elements_with_priority.sort(key=lambda x: x[1], reverse=True)
        
        return [elem for elem, _ in elements_with_priority[:max_elements]]

    
    def extract_accessibility_tree(self) -> Dict[str, Any]:
        """
        Extract accessibility tree similar to what screen readers use.
        This is the most efficient method - 90% smaller than full HTML.
        """
        tree = {
            "type": "accessibility_tree",
            "nodes": []
        }
        
        # Start from body or html
        root = self.soup.find('body') or self.soup.find('html')
        if root:
            tree["nodes"] = self._build_accessibility_tree(root)
        
        return tree
    
    def _build_accessibility_tree(self, element, depth=0, max_depth=10) -> List[Dict[str, Any]]:
        """Recursively build accessibility tree"""
        if depth > max_depth:
            return []
        
        nodes = []
        
        # Check if element is interactive or has semantic meaning
        if self._is_accessible_element(element):
            node = {
                "role": self._get_role(element),
                "name": self._get_accessible_name(element),
                "tag": element.name,
                "attributes": self._extract_important_attributes(element),
                "text": self._get_text_content(element),
                "selector": self._generate_selector(element)
            }
            
            # Add children
            children = []
            for child in element.children:
                if hasattr(child, 'name'):  # Is a tag, not text
                    children.extend(self._build_accessibility_tree(child, depth + 1, max_depth))
            
            if children:
                node["children"] = children
            
            nodes.append(node)
        else:
            # Not accessible itself, but check children
            for child in element.children:
                if hasattr(child, 'name'):
                    nodes.extend(self._build_accessibility_tree(child, depth, max_depth))
        
        return nodes
    
    def extract_interactive_elements_only(self) -> List[Dict[str, Any]]:
        """
        Extract only interactive elements in a flat list.
        Fastest method for simple use cases.
        """
        elements = []
        
        for selector in self.INTERACTIVE_SELECTORS:
            found_elements = self.soup.select(selector)
            for element in found_elements:
                if self._is_visible(element):
                    elem_data = {
                        "tag": element.name,
                        "text": self._get_text_content(element),
                        "attributes": self._extract_important_attributes(element),
                        "selector": self._generate_selector(element),
                        "context": self._get_element_context(element)
                    }
                    elements.append(elem_data)
        
        return elements
    
    def _extract_element_data(self, element) -> Optional[Dict[str, Any]]:
        """Extract comprehensive data for a single element"""
        try:
            return {
                "tag": element.name,
                "text": self._get_text_content(element),
                "attributes": self._extract_important_attributes(element),
                "selector": self._generate_selector(element),
                "xpath": self._generate_xpath(element),
                "context": self._get_element_context(element),
                "role": self._get_role(element),
                "accessible_name": self._get_accessible_name(element)
            }
        except Exception:
            return None
    
    def _is_likely_interactive(self, element) -> bool:
        """Check if element is likely interactive based on multiple signals (for full coverage)"""
        # Already in interactive selectors
        if element.name in ['button', 'a', 'input', 'select', 'textarea']:
            return True
        
        # Has click handler
        if element.get('onclick'):
            return True
        
        # Has role
        if element.get('role'):
            return True
        
        # Has tabindex (focusable)
        if element.get('tabindex'):
            return True
        
        # Has ARIA attributes
        if any(element.get(attr) for attr in ['aria-label', 'aria-labelledby', 'aria-expanded']):
            return True
        
        # Likely clickable based on class name
        classes = ' '.join(element.get('class', [])) if element.get('class') else ''
        clickable_patterns = ['click', 'button', 'btn', 'link', 'menu', 'nav', 'logo', 'banner', 'card', 'item']
        if any(pattern in classes.lower() for pattern in clickable_patterns):
            return True
        
        # Images (often clickable)
        if element.name in ['img', 'svg']:
            return True
        
        # Common semantic elements
        if element.name in ['nav', 'header', 'footer', 'li']:
            return True
        
        return False

    def _is_visible(self, element) -> bool:
        """Check if element is likely visible (basic heuristic)"""
        # Check for hidden attributes
        if element.get('hidden') or element.get('aria-hidden') == 'true':
            return False
        
        # Check style attribute for display:none or visibility:hidden
        style = element.get('style', '')
        if 'display:none' in style.replace(' ', '') or 'visibility:hidden' in style.replace(' ', ''):
            return False
        
        # Check class for common hidden patterns
        classes = element.get('class', [])
        if isinstance(classes, list):
            hidden_patterns = ['hidden', 'hide', 'invisible', 'aok-hidden']
            if any(pattern in ' '.join(classes) for pattern in hidden_patterns):
                return False
        
        return True
    
    def _is_accessible_element(self, element) -> bool:
        """Check if element should be in accessibility tree"""
        # Interactive elements
        if element.name in ['button', 'a', 'input', 'select', 'textarea']:
            return True
        
        # Elements with roles
        if element.get('role'):
            return True
        
        # Elements with ARIA labels
        if element.get('aria-label') or element.get('aria-labelledby'):
            return True
        
        # Elements with test IDs
        if element.get('data-testid') or element.get('data-test') or element.get('data-cy'):
            return True
        
        # Semantic HTML5 elements
        if element.name in ['nav', 'main', 'header', 'footer', 'article', 'section']:
            return True
        
        return False
    
    def _get_role(self, element) -> str:
        """Get ARIA role or implicit role"""
        # Explicit role
        if element.get('role'):
            return element.get('role')
        
        # Implicit roles based on tag
        implicit_roles = {
            'button': 'button',
            'a': 'link',
            'input': 'textbox',
            'select': 'combobox',
            'textarea': 'textbox',
            'nav': 'navigation',
            'main': 'main',
            'header': 'banner',
            'footer': 'contentinfo'
        }
        
        return implicit_roles.get(element.name, element.name)
    
    def _get_accessible_name(self, element) -> str:
        """Get accessible name (what screen readers announce)"""
        # Priority order for accessible name
        
        # 1. aria-label
        if element.get('aria-label'):
            return element.get('aria-label')
        
        # 2. aria-labelledby
        if element.get('aria-labelledby'):
            label_id = element.get('aria-labelledby')
            label_elem = self.soup.find(id=label_id)
            if label_elem:
                return self._get_text_content(label_elem)
        
        # 3. For inputs, check associated label
        if element.name == 'input' and element.get('id'):
            label = self.soup.find('label', {'for': element.get('id')})
            if label:
                return self._get_text_content(label)
        
        # 4. title attribute
        if element.get('title'):
            return element.get('title')
        
        # 5. alt attribute (for images)
        if element.get('alt'):
            return element.get('alt')
        
        # 6. value attribute (for buttons/inputs)
        if element.get('value'):
            return element.get('value')
        
        # 7. Text content
        return self._get_text_content(element)
    
    def _get_text_content(self, element) -> str:
        """Get clean text content of element"""
        text = element.get_text(strip=True)
        # Limit length
        return text[:200] if text else ""
    
    def _extract_important_attributes(self, element) -> Dict[str, str]:
        """Extract only important attributes"""
        attrs = {}
        for attr in self.IMPORTANT_ATTRIBUTES:
            value = element.get(attr)
            if value:
                # Handle class as string
                if attr == 'class' and isinstance(value, list):
                    attrs[attr] = ' '.join(value)
                else:
                    attrs[attr] = str(value)
        return attrs
    
    def _generate_selector(self, element) -> str:
        """Generate a CSS selector for the element"""
        # Priority: ID > data-testid > name > class + tag
        
        if element.get('id'):
            return f"#{element.get('id')}"
        
        if element.get('data-testid'):
            return f"[data-testid='{element.get('data-testid')}']"
        
        if element.get('name'):
            return f"{element.name}[name='{element.get('name')}']"
        
        # Use tag + first class
        classes = element.get('class', [])
        if classes:
            first_class = classes[0] if isinstance(classes, list) else classes
            return f"{element.name}.{first_class}"
        
        return element.name
    
    def _generate_xpath(self, element) -> str:
        """Generate XPath for element"""
        components = []
        child = element
        
        for parent in element.parents:
            siblings = parent.find_all(child.name, recursive=False)
            if len(siblings) > 1:
                index = siblings.index(child) + 1
                components.append(f"{child.name}[{index}]")
            else:
                components.append(child.name)
            child = parent
            
            # Stop at body or after 10 levels
            if parent.name == 'body' or len(components) >= 10:
                break
        
        components.reverse()
        return '//' + '/'.join(components) if components else f'//{element.name}'
    
    def _get_element_context(self, element) -> Dict[str, Any]:
        """Get context information about element's position in DOM"""
        parent = element.parent
        siblings = list(element.next_siblings) + list(element.previous_siblings)
        
        # Filter to only tag siblings
        tag_siblings = [s for s in siblings if hasattr(s, 'name')][:3]
        
        return {
            "parent": parent.name if parent else None,
            "parent_id": parent.get('id') if parent else None,
            "parent_class": parent.get('class') if parent else None,
            "sibling_count": len(tag_siblings),
            "siblings": [s.name for s in tag_siblings]
        }


def extract_from_html(html: str, method: str = "semantic") -> Dict[str, Any]:
    """
    Convenience function to extract DOM information.
    
    Args:
        html: HTML content
        method: 'semantic', 'accessibility', or 'interactive'
    
    Returns:
        Extracted DOM data
    """
    extractor = DOMExtractor(html)
    
    if method == "semantic":
        return extractor.extract_semantic_dom()
    elif method == "accessibility":
        return extractor.extract_accessibility_tree()
    elif method == "interactive":
        return {"elements": extractor.extract_interactive_elements_only()}
    else:
        raise ValueError(f"Unknown method: {method}")


# Example usage
if __name__ == "__main__":
    # Test with sample HTML
    sample_html = """
    <html>
        <body>
            <nav>
                <a href="/home">Home</a>
                <a href="/about">About</a>
            </nav>
            <main>
                <button id="submit-btn" data-testid="submit-button" aria-label="Submit Form">Submit</button>
                <input type="text" name="username" placeholder="Enter username" />
                <div class="hidden">Hidden content</div>
            </main>
        </body>
    </html>
    """
    
    # Test semantic extraction
    extractor = DOMExtractor(sample_html)
    semantic = extractor.extract_semantic_dom()
    print("Semantic DOM:")
    print(json.dumps(semantic, indent=2))
    
    print("\n" + "="*60 + "\n")
    
    # Test accessibility tree
    accessibility = extractor.extract_accessibility_tree()
    print("Accessibility Tree:")
    print(json.dumps(accessibility, indent=2))