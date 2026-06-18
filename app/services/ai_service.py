import os
import json
from openai import OpenAI
from dotenv import load_dotenv

# Load env variables
load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")
api_base = os.getenv("OPENAI_API_BASE")
model_name = os.getenv("OPENAI_MODEL", "gemini-3.5-flash")

# Initialize OpenAI client (could map to Gemini's compatibility base)
client = OpenAI(
    api_key=api_key,
    base_url=api_base if api_base else None
)

def evaluate_match(resume_data: dict, jd_text: str) -> dict:
    """Compare resume data and job description to calculate a match score and details."""
    resume_summary = f"""
Name: {resume_data.get('name')}
Title: {resume_data.get('title')}
Summary: {resume_data.get('summary')}
Skills: {json.dumps(resume_data.get('skills', {}))}
Experience Roles: {[exp['role'] + ' at ' + exp['company'] for exp in resume_data.get('experience', [])]}
"""
    prompt = f"""
You are an expert technical recruiter and ATS analyzer. Compare this candidate profile with the Job Description (JD).
Evaluate the match percentage, key skill overlaps, missing key terms/keywords, Candidate Strengths, and Gap Analysis.

Candidate Profile:
{resume_summary}

Job Description:
{jd_text}

Provide your response in EXACTLY the following JSON format:
{{
  "match_score": 85,
  "matching_skills": ["Python", "SQL", "ETL"],
  "missing_keywords": ["Spark", "Docker"],
  "relocation_notes": "Mention if the job is remote, hybrid, local, or requires relocation.",
  "strengths": "Provide a 2-3 sentence overview of why the candidate is a strong fit.",
  "gaps_analysis": "Identify key gaps between candidates skills and the requirements."
}}
Ensure the score is an integer between 0 and 100.
Do not wrap your output in markdown code blocks like ```json ... ```. Just return raw JSON.
"""
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "You are a helpful assistant that returns only raw JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2
        )
        content = response.choices[0].message.content.strip()
        # Fallback cleaning if markdown wrappers were included
        if content.startswith("```"):
            lines = content.splitlines()
            if lines[0].startswith("```json") or lines[0].startswith("```"):
                content = "\n".join(lines[1:-1]).strip()
        return json.loads(content)
    except Exception as e:
        print(f"Error evaluating match: {e}")
        return {
            "match_score": 50,
            "matching_skills": [],
            "missing_keywords": [],
            "relocation_notes": "Unable to extract.",
            "strengths": "Error during analysis.",
            "gaps_analysis": "Error during analysis."
        }

def tailor_resume(resume_data: dict, jd_text: str) -> dict:
    """Rewrite experience bullets to align with the job description without making up achievements."""
    prompt = f"""
You are an expert resume writer. Rewrite the professional experience bullets of the candidate's resume to highlight skills and projects relevant to the provided Job Description.
RULES:
1. DO NOT fabricate any work history, dates, companies, or metrics.
2. Align the framing and emphasis of bullets to match high-frequency and important keywords from the JD (e.g. data governance, ETL speed, ML forecasting).
3. Keep the same structure: list of companies, roles, and their corresponding bullet points.

Original Resume Experience:
{json.dumps(resume_data.get('experience', []), indent=2)}

Job Description:
{jd_text}

Provide your response in EXACTLY the following JSON format:
[
  {{
    "role": "Role Name",
    "company": "Company Name",
    "location": "Location",
    "date": "Date Range",
    "bullets": [
      "Rewritten bullet 1 emphasizing keywords",
      "Rewritten bullet 2 emphasizing keywords"
    ]
  }},
  ...
]
Do not wrap your output in markdown code blocks. Just return raw JSON.
"""
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "You are a helpful assistant that returns only raw JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3
        )
        content = response.choices[0].message.content.strip()
        if content.startswith("```"):
            lines = content.splitlines()
            if lines[0].startswith("```json") or lines[0].startswith("```"):
                content = "\n".join(lines[1:-1]).strip()
        return json.loads(content)
    except Exception as e:
        print(f"Error tailoring resume: {e}")
        return resume_data.get('experience', [])

def generate_cover_letter(resume_data: dict, company_name: str, job_title: str, jd_text: str) -> str:
    """Generate a highly personalized 3-paragraph cover letter."""
    prompt = f"""
You are a career coach. Write a compelling, concise cover letter (exactly 3 paragraphs, under 350 words total) for a candidate applying to a job.
Paragraph 1: Hook showing enthusiasm for the role and the company.
Paragraph 2: Directly link candidate's portfolio projects (e.g. IoT fleet telemetry, tax anomaly detection, or fintech credit fraud center) to the technical challenges described in the JD.
Paragraph 3: Professional call-to-action and closing.

Candidate Info:
Name: {resume_data.get('name')}
Profile: {resume_data.get('summary')}
Recent Projects: IoT telematics anomaly detection, Credit risk command center, Benford's Law tax audit pipeline.

Target Company: {company_name}
Target Role: {job_title}
Job Description:
{jd_text}

Write in a professional, confident, and human tone. Do not use generic buzzwords or robotic transitions.
Return ONLY the cover letter text, no metadata or greetings.
"""
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "You are a professional cover letter writer."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.5
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error generating cover letter: {e}")
        return f"Dear Hiring Team at {company_name},\n\nI am writing to express my interest in the {job_title} position. Given my background in data engineering and machine learning, I am confident I can add value to your team.\n\nSincerely,\n{resume_data.get('name')}"

def generate_outreach_templates(company_name: str, job_title: str, jd_text: str, recruiter_name: str = None) -> tuple[str, str]:
    """Generate connection notes and InMail outreach drafts."""
    recruiter = recruiter_name if recruiter_name else "hiring team member"
    prompt = f"""
You are an expert networker. Write two outreach message drafts to a recruiter/hiring manager at {company_name} for the {job_title} role.

Job Details:
Company: {company_name}
Title: {job_title}
JD Summary: {jd_text[:1000]}
Recruiter Name: {recruiter}

Draft 1 (LinkedIn Connection Request Note): Must be strict, high-converting, and under 300 characters.
Draft 2 (LinkedIn InMail/Email note): Under 150 words, highlighting relevant data/AI engineering credentials.

Provide your response in EXACTLY the following JSON format:
{{
  "short_note": "Draft 1 connection note text here...",
  "long_note": "Draft 2 InMail note text here..."
}}
Ensure "short_note" is under 300 characters.
Do not wrap your output in markdown code blocks. Just return raw JSON.
"""
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "You are a helpful assistant that returns only raw JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.4
        )
        content = response.choices[0].message.content.strip()
        if content.startswith("```"):
            lines = content.splitlines()
            if lines[0].startswith("```json") or lines[0].startswith("```"):
                content = "\n".join(lines[1:-1]).strip()
        data = json.loads(content)
        return data.get("short_note", ""), data.get("long_note", "")
    except Exception as e:
        print(f"Error generating outreach notes: {e}")
        fallback_short = f"Hi {recruiter}, I applied for the {job_title} role at {company_name}. I have 5+ years of experience in Python, SQL, and data engineering pipelines, and would love to connect to discuss how I can help your team."
        fallback_long = f"Dear {recruiter},\n\nI hope this message finds you well. I recently submitted my application for the {job_title} position at {company_name}.\n\nWith my background in building automated ETL pipelines and optimizing ML models, I am excited about the opportunity to bring these skills to your data team. I would welcome the chance to speak further.\n\nBest regards,\nDeshraj Jogiya"
        return fallback_short, fallback_long
