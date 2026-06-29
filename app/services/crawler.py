import urllib.request
import re
import html
import json
from datetime import datetime, timedelta
from sqlalchemy import func
from sqlalchemy.orm import Session
from ..models import JobApplication, SearchKeyword
from . import ai_service
from .activity_logger import log_activity

def clean_html(raw_html):
    """Remove HTML tags and clean up whitespace."""
    cleanr = re.compile('<.*?>')
    cleantext = re.sub(cleanr, '', raw_html)
    return html.unescape(cleantext).strip()

def scrape_job_description(job_url):
    """Fetch and parse the full job description from the public LinkedIn job page."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }
    req = urllib.request.Request(job_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            page_source = response.read().decode('utf-8', errors='ignore')
            
            # Find the description block
            # Public/guest jobs usually put this in class="show-more-less-html__markup"
            desc_match = re.search(r'<div class="show-more-less-html__markup[^"]*">(.*?)</div>', page_source, re.DOTALL)
            if desc_match:
                return clean_html(desc_match.group(1))
            
            # Fallback patterns
            desc_match_2 = re.search(r'<div class="description__text[^"]*">(.*?)</div>', page_source, re.DOTALL)
            if desc_match_2:
                return clean_html(desc_match_2.group(1))
                
            return "Description text could not be extracted automatically."
    except Exception as e:
        print(f"Error scraping job description for {job_url}: {e}")
        return "Failed to download description."

def is_within_timeframe(datetime_text: str, time_text: str, timeframe: str) -> bool:
    """Check if the job posting falls within the requested timeframe string."""
    if not datetime_text:
        return True # Default to include if date parsing fails
        
    try:
        posted_date = datetime.strptime(datetime_text, "%Y-%m-%d").date()
        current_date = datetime.now().date()
        delta_days = (current_date - posted_date).days
        
        tf = timeframe.lower().strip()
        if tf == "2h":
            return delta_days == 0 and ("hour" in time_text or "minute" in time_text)
        elif tf == "12h":
            return delta_days == 0
        elif tf == "24h":
            return delta_days <= 1
        elif tf == "3d":
            return delta_days <= 3
        elif tf == "5d":
            return delta_days <= 5
        elif tf == "7d":
            return delta_days <= 7
        elif tf == "3w":
            return delta_days <= 21
        elif tf == "1m":
            return delta_days <= 30
            
    except Exception:
        pass
    return True

def search_linkedin_jobs(keywords="Data Engineer", location="United States", limit=10):
    """Query LinkedIn public guest search for recent job postings."""
    url_keywords = urllib.parse.quote(keywords)
    url_location = urllib.parse.quote(location)
    url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={url_keywords}&location={url_location}&f_TPR=r2592000&start=0"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }
    req = urllib.request.Request(url, headers=headers)
    jobs_list = []
    
    try:
        print(f"Searching LinkedIn guest jobs via: {url}")
        with urllib.request.urlopen(req, timeout=10) as response:
            html_content = response.read().decode('utf-8', errors='ignore')
            
            # Split HTML by list items to parse each card independently
            card_blocks = html_content.split('<li')
            count = 0
            for block in card_blocks[1:]:
                # Extract URL & title
                url_match = re.search(r'<a class="base-card__full-link[^"]*" href="([^"]+)"', block)
                title_match = re.search(r'<span class="sr-only">\s*([^\n<]+)\s*</span>', block)
                company_match = re.search(r'<h4 class="base-search-card__subtitle">\s*<a[^>]*>\s*([^\n<]+)\s*</a>', block)
                if not company_match:
                    company_match = re.search(r'<h4 class="base-search-card__subtitle">\s*([^\n<]+)\s*</h4>', block)
                
                # Extract posting time
                time_match = re.search(r'<time[^>]*class="[^"]*"[^>]*>\s*([^\n<]+)\s*</time>', block)
                datetime_match = re.search(r'<time[^>]*datetime="([^"]+)"', block)
                
                if url_match and title_match:
                    url = url_match.group(1).split('?')[0]
                    title = clean_html(title_match.group(1))
                    company = clean_html(company_match.group(1)) if company_match else "Unknown Company"
                    
                    time_text = clean_html(time_match.group(1)) if time_match else ""
                    datetime_text = datetime_match.group(1).strip() if datetime_match else ""
                    
                    jobs_list.append({
                        "title": title,
                        "company": company,
                        "url": url,
                        "time_text": time_text,
                        "datetime_text": datetime_text
                    })
                    count += 1
                    if count >= limit:
                        break
                        
    except Exception as e:
        print(f"Error querying guest search: {e}")
        
    return jobs_list

def run_daily_crawl_and_ingest(db: Session, base_resume: dict, timeframe: str = "1m"):
    """Main pipeline execution for job search and matching ingestion."""
    log_activity(db, f"Initiating active job crawler query (Timeframe: {timeframe})...", "INFO")
    
    # Load keywords from database configuration
    db_queries = db.query(SearchKeyword).filter(SearchKeyword.is_active == True).all()
    if db_queries:
        search_queries = [q.keyword for q in db_queries]
        log_activity(db, f"Loaded {len(search_queries)} search keywords from configurations.", "INFO")
    else:
        search_queries = ["Machine Learning Engineer", "Applied Machine Learning Scientist", "Data Engineer", "Data Scientist", "Data Analyst", "Business Intelligence Analyst", "Applied Scientist"]
        log_activity(db, "No custom search keywords configured. Using default target job roles.", "INFO")
    
    all_jobs_found = []
    # If timeframe is narrow, crawl up to 15 jobs per keyword, otherwise 6 is enough
    crawl_limit = 15 if timeframe in ["2h", "12h", "24h", "3d"] else 6
    
    for query in search_queries:
        log_activity(db, f"Crawling public job posts for: '{query}'...", "INFO")
        jobs = search_linkedin_jobs(keywords=query, limit=crawl_limit)
        all_jobs_found.extend(jobs)
        
    # Filter by timeframe
    filtered_jobs = []
    for j in all_jobs_found:
        if is_within_timeframe(j["datetime_text"], j["time_text"], timeframe):
            filtered_jobs.append(j)
            
    # Prioritize: Sort jobs by age (delta_days ascending) so that recent jobs (0-1 days) are processed first
    def get_job_priority_key(job):
        try:
            if not job["datetime_text"]:
                return 999
            posted_date = datetime.strptime(job["datetime_text"], "%Y-%m-%d").date()
            return (datetime.now().date() - posted_date).days
        except Exception:
            return 999
            
    filtered_jobs.sort(key=get_job_priority_key)
    
    log_activity(db, f"Job crawl complete. Found {len(all_jobs_found)} total, {len(filtered_jobs)} match timeframe. Evaluating duplicates...", "INFO")
    
    ingested_count = 0
    new_job_ids = []
    for job in filtered_jobs:
        # Check duplicate (by URL or normalized company name and job title)
        exists = db.query(JobApplication).filter(
            (JobApplication.job_url == job["url"]) | 
            (
                (func.lower(JobApplication.company_name) == job["company"].lower().strip()) & 
                (func.lower(JobApplication.job_title) == job["title"].lower().strip())
            )
        ).first()
        if exists:
            print(f"Skipping duplicate: {job['title']} at {job['company']}")
            continue
            
        print(f"Ingesting new role: {job['title']} at {job['company']}...")
        log_activity(db, f"Analyzing and ingesting: {job['title']} at {job['company']}...", "INFO")
        # Scrape full job description
        jd_text = scrape_job_description(job["url"])
        if not jd_text or len(jd_text) < 100:
            print(f"Skipping role due to description retrieval failure.")
            continue
            
        # Run match scoring
        match_data = ai_service.evaluate_match(base_resume, jd_text)
        short_note, long_note = ai_service.generate_outreach_templates(
            job["company"], job["title"], jd_text
        )
        
        # Save record
        job_app = JobApplication(
            company_name=job["company"],
            job_title=job["title"],
            job_url=job["url"],
            job_description=jd_text,
            match_score=match_data.get("match_score", 50),
            match_analysis=json.dumps(match_data),
            visa_sponsorship=match_data.get("visa_sponsorship", "Unknown"),
            status="Ingested",
            outreach_note_short=short_note,
            outreach_note_long=long_note
        )
        db.add(job_app)
        db.commit()
        db.refresh(job_app)
        ingested_count += 1
        log_activity(db, f"Ingested: {job['title']} at {job['company']} (Match score: {match_data.get('match_score', 50)}%)", "INFO")
        
        new_job_ids.append(job_app.id)
        
        # Respect the Gemini API Free Tier 15 RPM rate limit
        import time
        print("Pacing API requests: sleeping 5 seconds...")
        time.sleep(5)
        
    log_activity(db, f"Job search crawl finished. Ingested {ingested_count} new postings.", "INFO")
    
    # Auto-archive low score ingested jobs (< 65%, older than 1 day) or old stale ingested jobs (> 5 days old)
    try:
        log_activity(db, "Running pipeline quality checks and queue cleanup...", "INFO")
        
        # 1. Archive low match scores (older than 1 day to allow manual review of new ingestions)
        cutoff_low_score = datetime.now() - timedelta(days=1)
        low_score_jobs = db.query(JobApplication).filter(
            JobApplication.status == "Ingested",
            JobApplication.match_score < 65,
            JobApplication.created_at < cutoff_low_score
        ).all()
        
        for job in low_score_jobs:
            log_activity(db, f"Auto-archiving low match role: {job.job_title} at {job.company_name} (Score: {job.match_score}%)", "INFO")
            job.status = "Rejected"
            job.notes = (job.notes or "") + f"\n[Auto Archive - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]\nMoved to Rejected: Match score {job.match_score}% is below the 65% pipeline quality threshold.\n"
        
        # 2. Archive stale ingested jobs (older than 5 days)
        cutoff_date = datetime.now() - timedelta(days=5)
        stale_jobs = db.query(JobApplication).filter(
            JobApplication.status == "Ingested",
            JobApplication.created_at < cutoff_date
        ).all()
        
        for job in stale_jobs:
            log_activity(db, f"Auto-archiving stale ingested role: {job.job_title} at {job.company_name} (Scraped on: {job.created_at.strftime('%Y-%m-%d')})", "INFO")
            job.status = "Rejected"
            job.notes = (job.notes or "") + f"\n[Auto Archive - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]\nMoved to Rejected: Role was unscored/inactive in the Ingested queue for more than 5 days.\n"
            
        db.commit()
        log_activity(db, "Pipeline checks complete.", "INFO")
    except Exception as e:
        print(f"Error during auto-pruning: {e}")
        
    return new_job_ids

