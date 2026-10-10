ALTER TABLE companies ADD COLUMN career_source TEXT CHECK (career_source IS NULL OR career_source IN ('icici','wipro','idfc','futuresfirst','sapient','hotstar','dpworld','bofa','jpmorgan','morganstanley','barclays','nomura'));
CREATE UNIQUE INDEX companies_career_source ON companies(career_source) WHERE career_source IS NOT NULL;
