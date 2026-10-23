# SCIMaP maps

Code to generate the maps for the Science \& Community Impacts Mapping Project website ([https://scienceimpacts.org]([url](https://scienceimpacts.org/))).

The scripts are used to calculate the following for both the NIH and NSF:

* baseline level of funding, economic impact, and jobs averaged across FY20-FY24
* raw loss, economic loss, and job loss due to grant terminations
* raw loss, economic loss, and job loss due to the proposed budget cuts for FY27.

In addition, the IDC\_map script calculates the loss projected from the proposed 15% cap on indirect costs. All scripts have outputs grouped at the city, county, congressional district, and state levels.

All output files are stored in the output folder.

## Reproducing termination outputs offline

The termination notebooks read saved inputs only; source retrieval is separate from scientific execution. Capture a new immutable Grant Witness snapshot with:

```bash
python3 scripts/capture_termination_sources.py \
  --output /path/to/snapshots/<snapshot-id>
```

The capture command validates required columns and records retrieval time, row counts and checksums. Copy or link its two CSVs into a versioned input tree only after review. `scripts/run_termination_pipeline.py` then runs NIH, NSF and Combined in fresh R processes, records all input hashes, and reconciles every scientific output against an approved reference directory.

The repository supplies the versioned Grant Witness snapshots and maintained references. Two large inputs stay outside Git: the approved `county_commute_NIH.csv` and a saved copy of OSF `home_119_workTR.csv`. Do not substitute the older Drive `states.csv` or `tract_dictionary.csv` for the repository versions.

```bash
R_LIBS_USER=/path/to/pinned-r-library \
python3 scripts/run_termination_pipeline.py \
  --as-of 2026-09-25 \
  --county-commute /path/to/county_commute_NIH.csv \
  --district-commute /path/to/home_119_workTR.csv \
  --reference-output output \
  --output /tmp/termination-replay
```

The command refuses to overwrite its output. A successful run writes the generated CSVs and `run-report.json`; no notebook downloads or publishing occur. `external-inputs.lock.csv` rejects unapproved large inputs, while `r-version.txt` and `r-packages.lock.csv` validate the R environment.

For a new-data candidate, pass the immutable directory created by the capture command with `--source-snapshot`, and add `--comparison-mode report`. Report mode still enforces schemas, finite values, unique generated keys, input integrity and package locks, but records expected scientific changes from the prior approved output instead of requiring identical values.

Every run then applies `scripts/validate_termination_outputs.py` and stores the result under `validation` in `run-report.json`. Errors fail the run and block publication: source schema, grants duplicated by dictionary joins, a common reporting week, complete non-negative weekly series where overall loss never falls, state/county/district/city reconciliation, dollar conservation through commuter allocation, unique institution identities, and Combined = NIH + NSF. Warnings record unmatched locations (an error above a provisional 1% of dollars), institution/tract state conflicts, duplicate dictionary keys and drift from the approved output; freshness reports NIH and NSF separately, including whether the required data actually changed. Use `--validation report` only for regression replays of a fixture with known, accepted errors. The validator also runs on an existing run directory:

Add `--notify` to the capture and pipeline commands in scheduled runs to post to Slack through the incoming webhook in `SCIMAP_ALERT_WEBHOOK_URL`: a download, schema or execution failure, a run blocked by validation errors, or a run that passed with warnings (for example unmatched locations). A clean run posts nothing, and local runs post only when `--notify` is given. Pass the last approved raw snapshot as `--source-baseline` so Grant Witness column changes are compared with Grant Witness itself, not with the R-written copies under `data/`. Warnings are kept to items someone must decide or correct; unused duplicate dictionary names, and multiplier differences already explained by a state conflict, are recorded as information only. `--notify` without a Slack webhook URL fails before any work starts.

```bash
python3 scripts/validate_termination_outputs.py /tmp/termination-run \
  --as-of 2026-10-19 --source-dir /path/to/snapshots/<snapshot-id> \
  --reference-output output --report /tmp/validation.json
```

## Project Structure
```
├── data/                               # Raw data
│   ├── CD_pop2024.csv
│   ├── NIH_impact_cong.csv
│   ├── NIH_impact_county.csv    
│   ├── NIH_impact_state.csv
│   ├── cbsa2fipsxw_2023.csv
│   ├── city_pop_2024.csv
│   ├── county_pop.csv
│   ├── geoid_dictionary.csv
│   ├── geoid_pop.csv
│   ├── nsf_budg_table.csv   
│   ├── nsf_budg_table.csv
│   ├── missing_orgs_fixed.csv
│   ├── missing_tractid_org_fixed.csv
│   ├── nsf_terminations.csv
│   ├── nih_terminations.csv
│   ├── org_names_corrected.csv
│   ├── orgs.csv
│   ├── state_and_county_fips_master.csv
│   ├── state_mult_2025.csv
│   ├── state_mult_2026.csv
│   ├── state_pop.csv
│   ├── states.csv
│   ├── terminations_clean.csv
│   ├── tract_dictionary.csv
│   └── us_representatives_119th_congress.csv
├── nsf_data/                                  # Raw data for NSF
│   ├── NSF_addresses_US_tract_county_cong119.csv
│   └── nsf_grants.csv
├── IDC_map.Rmd                                # Generate figures and tables
├── NIH_baseline.Rmd                           # Baseline funding NIH
├── NIH_budget_fy27.Rmd                        # FY27 budget impact NIH
├── NIH_terminations.Rmd                       # Grant terminations NIH
├── NSF_baseline.Rmd                           # Baseline funding NSF
├── NSF_baseline_fy27.Rmd                      # Baseline funding NSF
├── NSF_budget_fy27.Rmd                        # FY27 budget impact NSF
├── NSF_terminations.Rmd                       # Grant terminations NSF
└── README.md
```
## Data

### \\data files

CD\_pop2024.csv: population size for each district

cbsa2fipsxw\_2023.csv: dictionary mapping counties (CountyFIPS) to metro- and micropolitan areas (CBSA\_FIPS)

city\_pop\_2024.csv: population size for CBSAs

county\_pop.csv: population size for counties

geoid\_dictionary\_july4.csv: dictionary mapping congressional districts by GEOID code to name with additional information about each 

nsf\_budg\_table.csv: annual budget for NSF by office/directorate and year

nsf\_terminations.csv: the latest pull of Grant Witness' NSF terminations data, used to generate the corresponding output file

nih\_terminations.csv: the latest pull of Grant Witness' NIH terminations data, used to generate the corresponding output file

geoid\_dictionary.csv: dictionary mapping congressional districts by GEOID code to name with additional information about each district

org\_names\_corrected.csv: helper file to clean organization names from Grant Witness

orgs.csv: dictionary of organization name, city, and state

state\_and\_county\_fips\_master.csv: dictionary that maps county FIPS codes to county names and states

state\_mult\_2025.csv: contains economic and job loss multipliers for FY2025 (used for IDC calculation), calculated by United for Medical Research

state\_mult\_2026.csv: contains economic and job loss multipliers for FY2026 (used for all NIH calculations except for IDC calculation), calculated by United for Medical Research

state\_pop.csv: the population size of states

states.csv: dictionary of state names

tract\_dictionary.csv: maps NIH lat/lon coordinates to census tract and county FIPS

us\_representatives\_119th\_congress.csv: dictionary with information about Congressional Districts

### \\nsf\_data files:

NSF\_addresses\_US\_tract\_county\_cong119.csv: maps NSF grants to census tracts

nsf\_grants.csv: formatted dataframe of historical NSF grants

### from OSF (accessed remotely and not in repository):
The following two files are too large to post to Github and are therefore stored on OSF

home\_119\_workTR: commuter flows with Congressional District Cong\_ORIGIN and census tract Cong\_DESTINATION

NIH\_raw: large file of active NIH grants for fiscal year 2024 downloaded from RePORTER
