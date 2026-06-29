import os
import json
from openai import OpenAI
from dotenv import load_dotenv

# Load env variables
load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")
api_base = os.getenv("OPENAI_API_BASE")
model_name = os.getenv("OPENAI_MODEL", "gemini-1.5-flash")

# Initialize OpenAI client
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
  "visa_sponsorship": "Check if the JD mentions H-1B visa sponsorship. Return 'Sponsors' (if explicitly willing to sponsor), 'No Sponsorship' (if explicitly stating they will not sponsor), or 'Unknown' (if not mentioned).",
  "strengths": "Provide a 2-3 sentence overview of why the candidate is a strong fit.",
  "gaps_analysis": "Identify key gaps between candidates skills and the requirements."
}}
Ensure the score is an integer between 0 and 100.
Do not wrap your output in markdown code blocks. Just return raw JSON.
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

def run_tailoring_pass(experience_list: list, jd_text: str) -> list:
    """Pass 1: Initial experience tailoring rewrite."""
    prompt = f"""
You are an expert resume writer. Rewrite the professional experience bullets of the candidate's resume to highlight skills and projects relevant to the provided Job Description.
RULES:
1. DO NOT fabricate any work history, dates, companies, or metrics. Keep all statements 100% genuine.
2. Align the framing and emphasis of bullets to match high-frequency and important keywords from the JD (e.g. data governance, ETL speed, ML forecasting).

Original Experience:
{json.dumps(experience_list, indent=2)}

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
      "Rewritten bullet 1",
      "Rewritten bullet 2"
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
        print(f"Error in tailoring pass 1: {e}")
        return experience_list

def verify_ats_score(experience_list: list, jd_text: str) -> dict:
    """Verify ATS match score and extract remaining keyword gaps."""
    prompt = f"""
You are an ATS parser. Analyze the following candidate experience description against the Job Description.
Calculate a compatibility score (0-100) and list any high-priority keywords from the JD that are still missing from the candidate experience.

Candidate Experience:
{json.dumps(experience_list, indent=2)}

Job Description:
{jd_text}

Provide your response in EXACTLY the following JSON format:
{{
  "score": 90,
  "missing_keywords": ["Spark", "Kubernetes"]
}}
Do not wrap your output in markdown code blocks. Just return raw JSON.
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
        if content.startswith("```"):
            lines = content.splitlines()
            if lines[0].startswith("```json") or lines[0].startswith("```"):
                content = "\n".join(lines[1:-1]).strip()
        return json.loads(content)
    except Exception as e:
        print(f"Error checking ATS score: {e}")
        return {"score": 75, "missing_keywords": []}

def run_refinement_pass(experience_list: list, jd_text: str, missing_keywords: list) -> list:
    """Pass 2/3: Refine experience bullets to weave in missing keywords naturally and genuinely."""
    prompt = f"""
You are an expert resume writer. Refine the experience bullet points to naturally and genuinely weave in the following missing keywords from the JD, without fabricating any details or history.
Only add the keywords if they align with the candidate's existing achievements (e.g. if the keyword is 'Docker' and a bullet mentions containerization or scaling, refine it to include 'Docker').

Missing Keywords to Inject: {json.dumps(missing_keywords)}

Original Experience:
{json.dumps(experience_list, indent=2)}

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
      "Refined bullet 1",
      "Refined bullet 2"
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
        print(f"Error in refinement pass: {e}")
        return experience_list

def tailor_resume(resume_data: dict, jd_text: str) -> dict:
    """Tailor experience bullets using a multi-pass loops to target a 95+ ATS score."""
    experience = resume_data.get('experience', [])
    
    # Pass 1: Initial tailoring
    print("AI Resume Optimization - Pass 1: Inital rewrite...")
    experience = run_tailoring_pass(experience, jd_text)
    
    # Loop for Multi-pass ATS target (max 2 additional passes)
    for pass_num in range(2):
        evaluation = verify_ats_score(experience, jd_text)
        score = evaluation.get("score", 70)
        missing = evaluation.get("missing_keywords", [])
        
        print(f"ATS Match Score after Pass {pass_num+1}: {score}%")
        if score >= 95 or not missing:
            print(f"Target score reached or no key gaps remaining.")
            break
            
        print(f"Attempting to inject missing keywords: {missing}")
        experience = run_refinement_pass(experience, jd_text, missing)
        
    return experience

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
You are an elite talent acquisition and networking specialist. Write two outreach message drafts to a recruiter at {company_name} for the {job_title} role.
The drafts must sound 100% human-written, highly professional, direct, and completely devoid of generic buzzwords or typical AI transition templates (DO NOT use "hope this finds you well", "I wanted to reach out", "my passion for", or "feel free to").

Candidate Profile:
- Name: Deshraj Jogiya
- Target: Data Engineer / Machine Learning Engineer
- Core Value: 5+ years of experience building real-time data pipelines (ETL/ELT), database optimization, and orchestration (Airflow/Prefect).
- Highlights: Custom IoT fleet telematics anomaly detector, high-throughput credit risk dashboard, and Benford's Law tax audit analysis pipelines.

Job Details:
- Company: {company_name}
- Title: {job_title}
- JD Context: {jd_text[:1000]}
- Recruiter Name: {recruiter}

Draft 1 (LinkedIn Connection request note):
- Must be under 300 characters.
- Short, punchy, professional. Introduce your data engineering background and note your application.

Draft 2 (LinkedIn InMail / Email body):
- Keep it under 150 words.
- Structure:
  1. Direct, clean opening: Reference the {job_title} opening at {company_name} and immediately highlight relevant technical alignment.
  2. The Hook: Explicitly state the candidate's value-add—specifically how their expertise in optimizing database latency, real-time telemetry, or ML pipeline orchestration solves concrete technical challenges.
  3. Actionable closing: Offer a brief call or ask to connect regarding technical credentials.

Provide your response in EXACTLY the following JSON format:
{{
  "short_note": "Draft 1 connection note text here...",
  "long_note": "Draft 2 InMail/Email text here..."
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

def classify_email_response(subject: str, body: str) -> dict:
    """Classify the company's email response intent using AI."""
    prompt = f"""
You are an AI assistant for a career job tracking system. Analyze the following email subject and body received from a company.
Classify the email into exactly ONE of the following categories:
1. "confirmation" - The company is confirming they received the job application.
2. "additional_requirements" - The company is asking for more info, transcripts, portfolio links, or questions to be answered.
3. "assessment" - The company is requesting you to complete an Online Assessment (OA), HackerRank, LeetCode test, or coding challenge.
4. "interview" - The company is inviting you to a phone screen, call scheduling, next-round interview, or chat.
5. "rejection" - The company is rejecting your application or stating they are moving forward with other candidates.
6. "other" - None of the above.

Email Subject:
{subject}

Email Body:
{body}

Provide your response in EXACTLY the following JSON format:
{{
  "intent": "rejection" | "interview" | "assessment" | "additional_requirements" | "confirmation" | "other",
  "reason": "Brief 1-sentence summary of the message context (e.g. 'Invited for 30-min phone screen with hiring manager')",
  "action_required": true | false
}}
Ensure you return only raw JSON. Do not wrap it in markdown code blocks.
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
        if content.startswith("```"):
            lines = content.splitlines()
            if lines[0].startswith("```json") or lines[0].startswith("```"):
                content = "\n".join(lines[1:-1]).strip()
        return json.loads(content)
    except Exception as e:
        print(f"Error classifying email response: {e}")
        # Return fallback parsing based on basic keywords if API fails
        intent = "other"
        combined = (subject + " " + body).lower()
        if any(x in combined for x in ["unfortunate", "not select", "not move forward", "pursue other"]):
            intent = "rejection"
        elif any(x in combined for x in ["schedule", "interview", "phone screen", "chat about"]):
            intent = "interview"
        elif any(x in combined for x in ["assessment", "hackerrank", "coding challenge", "test"]):
            intent = "assessment"
        elif any(x in combined for x in ["confirm", "thank you for applying", "received"]):
            intent = "confirmation"
        return {
            "intent": intent,
            "reason": "Fallback parsing due to AI exception.",
            "action_required": intent in ["interview", "assessment", "additional_requirements"]
        }

