import urllib.request
import re
import html
import json
from datetime import datetime
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

def search_linkedin_jobs(keywords="Data Engineer", location="United States", limit=5):
    """Query LinkedIn public guest search for recent job postings."""
    # f_TPR=r604800 restricts to past 24 * 7 * 3600 seconds (past 7 days)
    # start=0 lists starting page
    url_keywords = urllib.parse.quote(keywords)
    url_location = urllib.parse.quote(location)
    url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={url_keywords}&location={url_location}&f_TPR=r604800&start=0"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }
    req = urllib.request.Request(url, headers=headers)
    jobs_list = []
    
    try:
        print(f"Searching LinkedIn guest jobs via: {url}")
        with urllib.request.urlopen(req, timeout=10) as response:
            html_content = response.read().decode('utf-8', errors='ignore')
            
            # Use regex to find job listings
            # Each card usually contains an href link and some details
            cards = re.findall(r'<a class="base-card__full-link[^"]*" href="([^"]+)"[^>]*>.*?<span class="sr-only">\s*(.*?)\s*</span>', html_content, re.DOTALL)
            
            # Also find company names
            companies = re.findall(r'<h4 class="base-search-card__subtitle">\s*<a[^>]*>\s*(.*?)\s*</a>', html_content, re.DOTALL)
            if not companies:
                companies = re.findall(r'<h4 class="base-search-card__subtitle">\s*(.*?)\s*</h4>', html_content, re.DOTALL)
                
            count = 0
            for idx, (job_url, job_title) in enumerate(cards):
                if count >= limit:
                    break
                    
                company = "Unknown Company"
                if idx < len(companies):
                    company = clean_html(companies[idx])
                
                # Trim tracking query parameters from job URL
                clean_url = job_url.split('?')[0]
                
                jobs_list.append({
                    "title": clean_html(job_title),
                    "company": company,
                    "url": clean_url
                })
                count += 1
                
    except Exception as e:
        print(f"Error querying guest search: {e}")
        
    return jobs_list

def run_daily_crawl_and_ingest(db: Session, base_resume: dict):
    """Main pipeline execution for job search and matching ingestion."""
    log_activity(db, "Initiating active job crawler query...", "INFO")
    
    # Load keywords from database configuration
    db_queries = db.query(SearchKeyword).filter(SearchKeyword.is_active == True).all()
    if db_queries:
        search_queries = [q.keyword for q in db_queries]
        log_activity(db, f"Loaded {len(search_queries)} search keywords from configurations.", "INFO")
    else:
        search_queries = ["Machine Learning Engineer", "Applied Machine Learning Scientist", "Data Engineer", "Data Scientist", "Data Analyst", "Business Intelligence Analyst", "Applied Scientist"]
        log_activity(db, "No custom search keywords configured. Using default target job roles.", "INFO")
    
    all_jobs_found = []
    for query in search_queries:
        log_activity(db, f"Crawling public job posts for: '{query}'...", "INFO")
        jobs = search_linkedin_jobs(keywords=query, limit=3)
        all_jobs_found.extend(jobs)
        
    log_activity(db, f"Job crawl complete. Found {len(all_jobs_found)} potential roles. Evaluating duplicates and match criteria...", "INFO")
    
    ingested_count = 0
    for job in all_jobs_found:
        # Check duplicate
        exists = db.query(JobApplication).filter(
            JobApplication.company_name == job["company"],
            JobApplication.job_title == job["title"]
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
        ingested_count += 1
        log_activity(db, f"Ingested: {job['title']} at {job['company']} (Match score: {match_data.get('match_score', 50)}%)", "INFO")
        
        # Respect the Gemini API Free Tier 15 RPM rate limit
        import time
        print("Pacing API requests: sleeping 5 seconds...")
        time.sleep(5)
        
    log_activity(db, f"Job search crawl finished. Ingested {ingested_count} new postings.", "INFO")
