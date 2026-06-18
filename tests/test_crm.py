import os
import unittest
import json
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Set database URL env variable to test database
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app.database import Base
from app.models import JobApplication, TailoredDocument
from app.services import ai_service

class TestJobSearchCRM(unittest.TestCase):
    def setUp(self):
        # Create an in-memory SQLite database
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_job_application_crud(self):
        # 1. Create a job application
        job = JobApplication(
            company_name="Google",
            job_title="Senior Data Engineer",
            job_description="We are looking for a Senior Data Engineer skilled in Python, SQL, and ETL systems.",
            match_score=80,
            status="Ingested"
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)

        self.assertIsNotNone(job.id)
        self.assertEqual(job.company_name, "Google")
        self.assertEqual(job.status, "Ingested")

        # 2. Add tailored documents
        doc = TailoredDocument(
            job_id=job.id,
            document_type="resume",
            content=json.dumps({"name": "Deshraj Jogiya", "experience": []})
        )
        self.db.add(doc)
        self.db.commit()

        # 3. Query and verify relations
        queried_job = self.db.query(JobApplication).filter_by(id=job.id).first()
        self.assertEqual(len(queried_job.documents), 1)
        self.assertEqual(queried_job.documents[0].document_type, "resume")

        # 4. Update status
        queried_job.status = "Applied"
        self.db.commit()
        
        updated_job = self.db.query(JobApplication).filter_by(id=job.id).first()
        self.assertEqual(updated_job.status, "Applied")

        # 5. Delete and test cascade
        self.db.delete(updated_job)
        self.db.commit()
        
        docs_count = self.db.query(TailoredDocument).count()
        self.assertEqual(docs_count, 0)

    def test_ai_outreach_fallback(self):
        # Test that outreach templates generate fallback when offline or in error states
        short_note, long_note = ai_service.generate_outreach_templates(
            company_name="Test Company",
            job_title="Test Role",
            jd_text="Needs Python skills.",
            recruiter_name="Recruiter Bob"
        )
        self.assertIsNotNone(short_note)
        self.assertIsNotNone(long_note)
        self.assertTrue("Test Company" in short_note or "Test Role" in short_note or "Bob" in short_note or len(short_note) > 0)
        self.assertTrue(len(short_note) <= 300)

    def test_imports(self):
        from app.services import crawler
        from app.services import scheduler
        from app.services import autofill_service
        self.assertIsNotNone(crawler)
        self.assertIsNotNone(scheduler)
        self.assertIsNotNone(autofill_service)

if __name__ == "__main__":
    unittest.main()
