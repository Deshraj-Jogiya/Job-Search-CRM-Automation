import urllib.request
import re
import html
import json
from datetime import datetime
from sqlalchemy.orm import Session
from ..models import JobApplication
from . import ai_service

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
    print("Initiating daily job crawler query...")
    search_queries = ["Data Engineer", "Machine Learning Engineer"]
    
    all_jobs_found = []
    for query in search_queries:
        jobs = search_linkedin_jobs(keywords=query, limit=3)
        all_jobs_found.extend(jobs)
        
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
            status="Ingested",
            outreach_note_short=short_note,
            outreach_note_long=long_note
        )
        db.add(job_app)
        db.commit()
        ingested_count += 1
        
    print(f"Job search finished. Ingested {ingested_count} new postings.")
