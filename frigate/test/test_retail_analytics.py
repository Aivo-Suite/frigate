import os
import sys
import unittest
from unittest.mock import patch, MagicMock
import numpy as np

# Force testing with an in-memory SQLite database before any module is loaded
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

# Append project root to path for imports to work correctly
sys.path.append(os.path.join(os.path.dirname(__file__), '../../'))

from retail_analytics import db_config
from retail_analytics import analytics_daemon

class TestRetailAnalyticsDaemon(unittest.TestCase):
    
    def setUp(self):
        """Set up an isolated in-memory database and clean known faces before each test."""
        db_config.init_db()
        analytics_daemon.known_faces.clear()
        analytics_daemon.last_heatmap_time.clear()

    def test_db_initialization_sqlite(self):
        """Test if SQLAlchemy correctly creates tables in SQLite."""
        with db_config.engine.connect() as conn:
            # Check if tables exist
            result = conn.execute(db_config.text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
            tables = [row[0] for row in result]
            
            self.assertIn('visits', tables)
            self.assertIn('face_embeddings', tables)
            self.assertIn('watch_list', tables)
            self.assertIn('heatmap_points', tables)

    def test_visit_duration_calculation(self):
        """Test if dwell time is calculated correctly based on start and end events."""
        event_payload = {
            "type": "end",
            "after": {
                "id": "track123",
                "label": "person",
                "start_time": 1000.0,
                "camera": "test_cam"
            }
        }
        
        # We need to mock time.time() inside save_visit, which is where it computes end_time if missing.
        # Wait, the code says: end_time = event_data.get("type") == "end" and time.time() or None
        with patch('retail_analytics.analytics_daemon.time.time', return_value=1050.0):
            analytics_daemon.save_visit(event_payload)
            
        # Verify database insertion
        with db_config.engine.connect() as conn:
            visit = conn.execute(db_config.text("SELECT start_time, end_time, dwell_time_seconds, camera_name FROM visits WHERE tracking_id = 'track123'")).fetchone()
            
            self.assertIsNotNone(visit)
            self.assertEqual(visit[0], 1000.0) # start
            self.assertEqual(visit[1], 1050.0) # end
            self.assertEqual(visit[2], 50.0)   # dwell_time
            self.assertEqual(visit[3], "test_cam")

    @patch('retail_analytics.analytics_daemon.DeepFace')
    @patch('retail_analytics.analytics_daemon.requests.get')
    def test_face_clustering(self, mock_requests_get, mock_deepface):
        """Test the Zero-Shot Re-ID cosine similarity clustering logic."""
        # 1. Mock the API request to return a fake image byte array
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b'fake_image_data'
        mock_requests_get.return_value = mock_response

        # 2. Mock DeepFace behavior
        # First visitor embedding
        first_embedding = np.random.rand(128).tolist()
        # Second visitor embedding (almost identical to first)
        second_embedding = first_embedding.copy()
        
        mock_deepface.represent.return_value = [{"embedding": first_embedding}]
        mock_deepface.analyze.return_value = [{"age": 30, "dominant_gender": "Man"}]

        # Emulate tracking ID 1 (creates a new visitor)
        face_id_1, age_1, gender_1 = analytics_daemon.process_reid("track_person_1")
        
        self.assertIsNotNone(face_id_1)
        self.assertTrue(face_id_1.startswith("VISITOR_"))
        self.assertEqual(age_1, 30)
        self.assertEqual(gender_1, "Man")
        
        # Emulate tracking ID 2 with similar face (should cluster)
        mock_deepface.represent.return_value = [{"embedding": second_embedding}]
        face_id_2, age_2, gender_2 = analytics_daemon.process_reid("track_person_2")
        
        # The face_id should be exactly the same due to high similarity
        self.assertEqual(face_id_1, face_id_2)

    @patch('retail_analytics.analytics_daemon.requests.post')
    def test_watch_list_trigger(self, mock_post):
        """Test if the Telegram alert is dispatched when a VIP enters."""
        # Add VIP to watch_list in DB
        vip_id = "VISITOR_VIP123"
        with db_config.engine.begin() as conn:
            conn.execute(
                db_config.text("INSERT INTO watch_list (face_id, tag) VALUES (:fid, :tag)"),
                {"fid": vip_id, "tag": "VIP"}
            )
            
        # Trigger a visit with that face_id natively set by Frigate (to bypass Re-ID calculation)
        event_payload = {
            "type": "end",
            "after": {
                "id": "track999",
                "label": "person",
                "sub_label": vip_id, # Frigate native recognition
                "start_time": 1000.0,
                "camera": "front_door"
            }
        }
        
        # Set Telegram env vars so the function runs
        os.environ["TELEGRAM_BOT_TOKEN"] = "fake_token"
        os.environ["TELEGRAM_CHAT_ID"] = "12345"
        analytics_daemon.TELEGRAM_BOT_TOKEN = "fake_token"
        analytics_daemon.TELEGRAM_CHAT_ID = "12345"

        analytics_daemon.save_visit(event_payload)
        
        # Assert the Telegram API was called
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertIn("https://api.telegram.org/botfake_token", args[0])
        self.assertEqual(kwargs['json']['chat_id'], "12345")
        self.assertIn("VISITOR_VIP123", kwargs['json']['text'])
        self.assertIn("VIP", kwargs['json']['text'])

if __name__ == '__main__':
    unittest.main()
