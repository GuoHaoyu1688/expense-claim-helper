# Version 0.1.1

Restore native CSV archives containing large receipt and claim files within the existing bounded import limit.

Local verification: 39 tests passed; 91% measured coverage. Ruff lint and formatting passed. Real workflow checks are recorded in the project documentation.

## Known limitations

One currency and fictional policy defaults. POSIX receipt handling; built-in PDF font supports Latin text. Claimed records and approval history cannot be reversed. Reminders run on query. No invoice authentication, company submission, or bank integration.
