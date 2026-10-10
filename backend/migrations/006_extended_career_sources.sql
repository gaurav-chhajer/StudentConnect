ALTER TABLE companies RENAME COLUMN career_source TO career_source_legacy;
ALTER TABLE companies ADD COLUMN career_source TEXT CHECK (career_source IS NULL OR career_source IN ('icici','wipro','idfc','futuresfirst','sapient','hotstar','dpworld','bofa','jpmorgan','morganstanley','barclays','nomura','postman','cvent','zs'));
UPDATE companies SET career_source=career_source_legacy;
CREATE UNIQUE INDEX companies_career_source_v2 ON companies(career_source) WHERE career_source IS NOT NULL;
