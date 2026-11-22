"""Suite boundaries shared by the Python unit runner and CI planner."""

# Dedicated end-to-end, integration, and image jobs supply their own setup.
UNIT_TEST_EXCLUDED_DIRS = frozenset({"docker", "e2e", "integration"})
