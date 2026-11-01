# Saved permissions for raw-stream v6

Source: `/home/thaddus/a-mem-variant/runs/gatemem-medical-raw-stream-v6/memory.privacy.sqlite`. Read-only query; 178 source turns ingested in the saved run summary.

These are the latest stored assignment records. Active status does not establish effective access on its own: restrictions, activity validity, source lifecycle, and runtime authorization still apply. Counts are exact bound-field entries per assignment, not unique facts across assignments.

| User | Scope | Status | Bound fields |
|---|---|---|---:|
| billing_cho | billing_insurance_statement_preferences | active | 11 |
| clinician_dr_shah | clinical_history_and_symptoms | active | 53 |
| family_linda_park | logistics_appointment_times_parking | revoked | 1 |
| family_linda_park | logistics_appointment_times_location | revoked | 10 |
| labtech_hassan | lab_order_details | active | 4 |
| nurse_alvarez | clinical_symptoms_bleeding | active | 21 |
| nurse_casey_liu | clinical_symptoms_bleeding | active | 6 |
| patient_elena_park | self-use: billing_registration | active | 1 |
| patient_elena_park | self-use: early_pregnancy_clinic_follow_up | active | 1 |
| patient_elena_park | self-use: early_pregnancy_clinic_follow_up | active | 1 |
| patient_elena_park | self-use: early_pregnancy_clinic_follow_up | active | 12 |
| patient_elena_park | self-use: early_pregnancy_clinic_follow_up | active | 21 |
| patient_elena_park | self-use: billing_registration | active | 7 |
| patient_elena_park | self-use: early_pregnancy_clinic_follow_up | active | 75 |

Observed speakers without an assignment:

- `agent_memcare`
- `clinician_dr_nguyen`
- `patient_mina_park`
- `pharmacist_owens`
- `reception_marta`
- `scheduler_jules`
- `social_worker_ramos`

The CSV contains the complete query result. The JSON expands full_details into readable objects, including bindings, evidence, actions, and validity. These files are snapshots and do not update automatically.
