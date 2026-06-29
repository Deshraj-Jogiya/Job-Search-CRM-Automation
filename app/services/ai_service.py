import os
import json
import re
import urllib.parse
import requests
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

def fetch_company_info(company_name: str) -> str:
    """Fetch search results for target company's mission and goals using DuckDuckGo HTML search."""
    try:
        query = urllib.parse.quote(f"{company_name} company mission motto values")
        url = f"https://html.duckduckgo.com/html/?q={query}"
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            html = res.text
            snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)
            clean_snippets = []
            for snip in snippets:
                text = re.sub(r'<[^>]+>', '', snip).strip()
                if text:
                    clean_snippets.append(text)
            if clean_snippets:
                return "\n- ".join(clean_snippets[:3])
    except Exception as e:
        print(f"Error fetching company search details: {e}")
    return f"Mission: Leading innovation in digital engineering and data solutions."

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
    """Tailor the entire resume context (experience, projects, summary, skills) to target a 95+ score and fit under a 2-page print layout."""
    prompt = f"""
You are an expert executive resume writer. Tailor the candidate's resume (summary, skills, experience, and projects) to perfectly align with the target Job Description (JD).
Your goal is to maximize ATS compatibility (target 95%+) while keeping the total length strictly under a 2-page print layout (concise, high-impact phrasing).

RULES:
1. DO NOT fabricate any work history, dates, companies, education, or credentials. Keep all statements genuine.
2. Select EXACTLY 3 projects from the candidate's projects list that are most relevant to the JD. Do not include more. For each selected project, rewrite its achievements into 2 concise, metrics-driven bullet points.
3. Tailor the professional experience bullets to weave in keywords from the JD (e.g. data pipelines, machine learning, cloud databases, dashboarding). Use action verbs and highlight metrics.
4. Keep bullets concise: max 3 bullet points per recent job, and max 2 bullets for older roles. This ensures the output fits within the 2-page limit.
5. Tailor the professional summary to align with target role priorities (e.g., machine learning focus vs. data engineering focus).

Input Resume Data:
{json.dumps(resume_data, indent=2)}

Target Job Description:
{jd_text}

Provide your response in EXACTLY the following JSON format:
{{
  "name": "Candidate Name",
  "title": "Tailored Professional Title",
  "contact": {{
    "location": "Location",
    "phone": "Phone",
    "email": "Email",
    "linkedin": "LinkedIn URL",
    "github": "GitHub URL",
    "portfolio": "Portfolio URL"
  }},
  "summary": "Tailored Professional Summary",
  "skills": {{
    "languages": ["Python", "SQL", ...],
    "ml_data_science": ["Random Forest", ...],
    "data_engineering_cloud": ["Snowflake", ...],
    "methodologies_tools": ["Git", ...]
  }},
  "experience": [
    {{
      "role": "Role Title",
      "company": "Company",
      "location": "Location",
      "date": "Date Range",
      "bullets": [
        "Tailored bullet 1",
        "Tailored bullet 2"
      ]
    }},
    ...
  ],
  "projects": [
    {{
      "name": "Project Name",
      "bullets": [
        "Tailored achievement bullet 1",
        "Tailored achievement bullet 2"
      ],
      "technologies": ["Spark", "Python", ...]
    }},
    ...
  ],
  "education": [ ... ],
  "certifications": [ ... ]
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
            temperature=0.3
        )
        content = response.choices[0].message.content.strip()
        if content.startswith("```"):
            lines = content.splitlines()
            if lines[0].startswith("```json") or lines[0].startswith("```"):
                content = "\n".join(lines[1:-1]).strip()
        tailored_data = json.loads(content)
        for key in ["contact", "education", "certifications"]:
            if key not in tailored_data and key in resume_data:
                tailored_data[key] = resume_data[key]
        if "projects" in tailored_data and isinstance(tailored_data["projects"], list):
            tailored_data["projects"] = tailored_data["projects"][:3]
            for proj in tailored_data["projects"]:
                if "bullets" not in proj and "description" in proj:
                    proj["bullets"] = [proj["description"]]
        return tailored_data
    except Exception as e:
        print(f"Error tailoring resume: {e}")
        return resume_data.copy()

def clean_cover_letter(text: str) -> str:
    if not text:
        return text
    lines = text.strip().split("\n")
    cleaned_lines = []
    
    for line in lines:
        l = line.strip().lower()
        if not l:
            cleaned_lines.append("")
            continue
        if l.startswith(("dear", "to the", "hello", "hi ", "attention:")) or l.endswith(("hiring team", "hiring manager", "team")):
            continue
        if l.startswith(("sincerely", "best regards", "warm regards", "respectfully", "thank you for your time", "thank you for considering", "thank you for the opportunity", "thanks,")):
            continue
        if "deshraj" in l or "jogiya" in l:
            continue
        cleaned_lines.append(line)
        
    cleaned_text = "\n".join(cleaned_lines).strip()
    while "\n\n\n" in cleaned_text:
        cleaned_text = cleaned_text.replace("\n\n\n", "\n\n")
    return cleaned_text

def generate_cover_letter(resume_data: dict, company_name: str, job_title: str, jd_text: str) -> str:
    """Generate a highly personalized 3-paragraph cover letter aligned with company mission and goals."""
    company_info = fetch_company_info(company_name)
    
    projects_list = resume_data.get("projects", [])
    projects_context = ""
    for proj in projects_list:
        name = proj.get("name", "")
        bullets = proj.get("bullets", [])
        if not bullets and "description" in proj:
            bullets = [proj["description"]]
        desc = " ".join(bullets)
        techs = ", ".join(proj.get("technologies", []))
        projects_context += f"- {name}: {desc} (Technologies: {techs})\n"
        
    prompt = f"""
You are an elite executive cover letter writer. Write an extremely professional, compelling, and tailored cover letter (exactly 3 paragraphs, under 300 words total) for a candidate applying to a job.
You must showcase the candidate's core expertise, explain why they are a great technical fit, and show genuine enthusiasm for the company by aligning the letter with the company's mission/motto/goals based on recent search context.

Candidate Info:
Name: {resume_data.get('name')}
Profile: {resume_data.get('summary')}
Recent Projects:
{projects_context}

Target Company: {company_name}
Recent Target Company Context/Mission/Motto:
{company_info}

Target Role: {job_title}
Job Description:
{jd_text}

Structure Rules:
- Paragraph 1: Direct hook expressing strong interest in the specific position at {company_name}. Align with {company_name}'s mission, core values, or recent achievements. Make it clear, showing genuine enthusiasm for why you want to work with them!
- Paragraph 2: Map the candidate's experience and specific projects (select 1 or 2 most relevant projects from the Recent Projects list) directly to the technical challenges/requirements of the job description. Show impact, numbers, and how your skills solve their specific problem.
- Paragraph 3: Reiterate value and provide a professional call-to-action to discuss next steps.

Tone Rules:
- Highly professional, authentic, persuasive, and human-like.
- Absolutely NO greetings (like "Dear...") or sign-offs (like "Sincerely...") because the template already renders them. Start directly with the body text of Paragraph 1 and end with Paragraph 3.
- Do NOT use repetitive phrasing, generic template language (e.g. "I am excited to apply", "Please find my resume attached", "My qualifications make me a unique fit"), or passive sentences. Write in an active, direct voice of a seasoned staff engineer.
"""
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "You are a professional cover letter writer. You return ONLY the 3 body paragraphs, without any greetings, closings, or signatures."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.5
        )
        raw_content = response.choices[0].message.content.strip()
        return clean_cover_letter(raw_content)
    except Exception as e:
        print(f"Error generating cover letter: {e}")
        fallback_para = (
            f"I am writing to express my strong interest in the {job_title} position at {company_name}. "
            f"With over 5 years of professional experience building scalable data pipelines (ETL/ELT), optimizing database schema designs, "
            f"and deploying machine learning models, I am confident in my ability to deliver immediate value to your engineering team."
            f"\n\n"
            f"Throughout my career, I have specialized in building real-time data ingestion streams, model tracking setups using MLflow, "
            f"and robust orchestration layers with Apache Airflow. I am eager to apply this hands-on technical expertise to solve the complex "
            f"data infrastructure and integration challenges present at {company_name}."
            f"\n\n"
            f"Thank you for considering my application. I would welcome the opportunity to discuss how my background in Python, "
            f"SQL, and distributed data systems aligns with your upcoming engineering priorities."
        )
        return fallback_para

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

