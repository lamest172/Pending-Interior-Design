import unittest
import os
import tempfile
from pathlib import Path

import database
from app import app


def make_test_db():
    f = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    f.close()
    return f.name


class TestDatabaseFunctions(unittest.TestCase):

    def setUp(self):
        self.db_path = make_test_db()
        self.original_path = database.DB_PATH
        database.DB_PATH = Path(self.db_path)
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.original_path
        os.unlink(self.db_path)

    def _make_project(self, title='Test Project'):
        return database.create_project(
            title=title,
            description='A sample project',
            category='Residential',
            style='Modern',
            property_type='HDB 4-Room'
        )

    def test_create_project_returns_id(self):
        """Creating a project returns a valid integer ID."""
        project_id = self._make_project()
        self.assertIsNotNone(project_id)
        self.assertGreater(project_id, 0)

    def test_get_project_returns_correct_title(self):
        """Fetching a project by ID returns the correct record."""
        project_id = self._make_project('My Project')
        project = database.get_project(project_id)
        self.assertIsNotNone(project)
        self.assertEqual(project['title'], 'My Project')

    def test_get_nonexistent_project_returns_none(self):
        """Fetching a project with an invalid ID returns None."""
        project = database.get_project(9999)
        self.assertIsNone(project)

    def test_toggle_visibility_hides_project(self):
        """Toggling visibility on a visible project hides it."""
        project_id = self._make_project()
        new_state = database.toggle_visibility(project_id)
        self.assertEqual(new_state, 0)

    def test_toggle_visibility_restores_project(self):
        """Toggling visibility twice restores the project to visible."""
        project_id = self._make_project()
        database.toggle_visibility(project_id)
        new_state = database.toggle_visibility(project_id)
        self.assertEqual(new_state, 1)

    def test_hidden_project_excluded_from_visible_list(self):
        """Hidden projects do not appear in the customer-facing listing."""
        project_id = self._make_project('Hidden Project')
        database.toggle_visibility(project_id)
        projects = database.get_visible_projects()
        titles = [p['title'] for p in projects]
        self.assertNotIn('Hidden Project', titles)

    def test_toggle_featured_sets_flag(self):
        """Toggling featured on a project sets is_featured to 1."""
        project_id = self._make_project()
        new_state = database.toggle_featured(project_id)
        self.assertEqual(new_state, 1)

    def test_toggle_favorite_saves_project(self):
        """Favouriting a project returns True and saves the record."""
        project_id = self._make_project()
        added = database.toggle_favorite('customer:test@test.com', project_id)
        self.assertTrue(added)

    def test_toggle_favorite_removes_project(self):
        """Favouriting an already-saved project removes it and returns False."""
        project_id = self._make_project()
        account = 'customer:test@test.com'
        database.toggle_favorite(account, project_id)
        removed = database.toggle_favorite(account, project_id)
        self.assertFalse(removed)

    def test_filter_by_style_returns_matching_projects(self):
        """Filtering by style only returns projects with that style."""
        self._make_project('Scandinavian Home')
        database.save_project(1, 'Scandinavian Home', '', 'Residential', 'Scandinavian', 'HDB')
        projects = database.get_visible_projects({'style': 'Modern'})
        for p in projects:
            self.assertEqual(p['style'], 'Modern')


class TestFlaskRoutes(unittest.TestCase):

    def setUp(self):
        self.db_path = make_test_db()
        self.original_path = database.DB_PATH
        database.DB_PATH = Path(self.db_path)
        database.init_db()
        app.config['TESTING'] = True
        app.config['SECRET_KEY'] = 'test-secret'
        self.client = app.test_client()

    def tearDown(self):
        database.DB_PATH = self.original_path
        os.unlink(self.db_path)

    def test_home_page_loads(self):
        """Home page returns HTTP 200."""
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)

    def test_login_page_loads(self):
        """Login page returns HTTP 200."""
        response = self.client.get('/login')
        self.assertEqual(response.status_code, 200)

    def test_nonexistent_project_returns_404(self):
        """Requesting a project that does not exist returns HTTP 404."""
        response = self.client.get('/idp/9999')
        self.assertEqual(response.status_code, 404)

    def test_designer_dashboard_loads(self):
        """Designer dashboard page returns HTTP 200."""
        response = self.client.get('/designer/dashboard')
        self.assertEqual(response.status_code, 200)

    def test_logout_redirects(self):
        """Logging out redirects the user."""
        response = self.client.get('/logout')
        self.assertEqual(response.status_code, 302)


if __name__ == '__main__':
    unittest.main(verbosity=2)
