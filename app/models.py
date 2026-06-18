from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from .database import Base

class CandidateAccount(Base):
    __tablename__ = "candidate_accounts"

    id = Column(Integer, primary_key=True, index=True)
    company_name = Column(String, nullable=False, index=True)
    login_url = Column(String, nullable=True)
    username = Column(String, nullable=False)
    password = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    applications = relationship("JobApplication", back_populates="account")

class JobApplication(Base):
    __tablename__ = "job_applications"

    id = Column(Integer, primary_key=True, index=True)
    company_name = Column(String, nullable=False, index=True)
    job_title = Column(String, nullable=False, index=True)
    job_url = Column(String, nullable=True)
    job_description = Column(Text, nullable=False)
    match_score = Column(Integer, default=0)
    match_analysis = Column(Text, nullable=True)  # JSON text representing matching/missing terms
    status = Column(String, default="Ingested")  # Ingested, Tailored, Applied, Interviewing, Offer, Rejected, Archived
    recruiter_name = Column(String, nullable=True)
    recruiter_linkedin = Column(String, nullable=True)
    outreach_note_short = Column(String(500), nullable=True)  # Under 300-char LinkedIn connection request
    outreach_note_long = Column(Text, nullable=True)          # LinkedIn InMail / Email draft
    created_at = Column(DateTime, default=datetime.utcnow)
    applied_at = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)

    # Portal Account Relation
    account_id = Column(Integer, ForeignKey("candidate_accounts.id"), nullable=True)
    account = relationship("CandidateAccount", back_populates="applications")

    # Relationships
    documents = relationship("TailoredDocument", back_populates="job", cascade="all, delete-orphan")

class TailoredDocument(Base):
    __tablename__ = "tailored_documents"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("job_applications.id"), nullable=False)
    document_type = Column(String, nullable=False)  # 'resume', 'cover_letter'
    content = Column(Text, nullable=False)          # HTML or markdown content
    generated_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    job = relationship("JobApplication", back_populates="documents")
