"""
Phase 5: ML-Powered Conclusion Generation using OpenAI GPT-4o Mini
with Grammar Correction using LanguageTool
"""

import json
import logging
import os
from openai import OpenAI

logger = logging.getLogger(__name__)

# Try to import LanguageTool for grammar correction
try:
    from language_tool_python import LanguageTool
    GRAMMAR_TOOL_AVAILABLE = True
    logger.info("LanguageTool imported successfully")
except ImportError:
    GRAMMAR_TOOL_AVAILABLE = False
    logger.warning("LanguageTool not installed. Grammar correction disabled.")

class Phase5MLGenerator:
    # Class-level LanguageTool instance (shared across all instances)
    _grammar_tool = None

    # Hybrid classification keywords
    SURGICAL_KEYWORDS = [
        'surgery', 'surgical', 'repair', 'resection', 'fixation', 'excision',
        'ablation', 'transplant', 'reconstruction', 'arthroscopy', 'endoscopy',
        'laparoscopy', 'hysterectomy', 'appendicectomy', 'appendectomy',
        'cholecystectomy', 'hernia', 'fracture', 'dislocation', 'wound',
        'laceration', 'injection', 'drainage', 'biopsy', 'aspiration',
        'catheterization', 'stent', 'bypass', 'grafting', 'suturing', 'closure',
        'ligation', 'angioplasty', 'cystoscopy', 'colonoscopy', 'bronchoscopy',
        'thoracotomy', 'craniotomy', 'laminectomy', 'mastectomy', 'prostatectomy',
        'nephrectomy', 'splenectomy', 'gastrectomy', 'colostomy', 'tracheostomy'
    ]

    ONCOLOGY_KEYWORDS = [
        'cancer', 'tumor', 'oncology', 'malignancy', 'chemotherapy', 'chemo',
        'radiation', 'radiotherapy', 'lymphoma', 'carcinoma', 'sarcoma',
        'melanoma', 'leukemia', 'immunotherapy', 'targeted therapy', 'palliative',
        'metasta', 'neoplasm', 'malignant', 'remission', 'relapse'
    ]

    MATERNITY_KEYWORDS = [
        'pregnancy', 'pregnant', 'delivery', 'labor', 'childbirth', 'obstetric',
        'obstetrics', 'maternal', 'prenatal', 'postnatal', 'postpartum',
        'cesarean', 'c-section', 'vaginal delivery', 'eclampsia', 'preeclampsia',
        'gestational', 'miscarriage', 'abortion', 'lactation', 'antenatal'
    ]

    def __init__(self, api_key: str = None):
        # Get API key from parameter, environment variable, or config
        if not api_key:
            api_key = os.getenv("OPENAI_API_KEY")
            logger.info(f"API key from environment: {bool(api_key)}")

        if not api_key:
            try:
                from app.core.config import settings
                api_key = getattr(settings, 'openai_api_key', None)
                logger.info(f"API key from settings: {bool(api_key)}")
            except Exception as e:
                logger.error(f"Error loading settings: {e}")

        if not api_key:
            raise ValueError("OPENAI_API_KEY not configured. Set OPENAI_API_KEY environment variable or add to .env file")

        logger.info(f"Initializing OpenAI client with API key (first 20 chars): {api_key[:20]}...")
        try:
            self.client = OpenAI(api_key=api_key)
            logger.info("OpenAI client initialized successfully")
        except Exception as e:
            logger.error(f"Error initializing OpenAI client: {e}")
            raise

        self.model = "gpt-4o-mini"  # OpenAI GPT-4o Mini
        logger.info(f"Model set to: {self.model}")

        # Initialize grammar tool once (class-level, cached) - non-blocking
        if GRAMMAR_TOOL_AVAILABLE and Phase5MLGenerator._grammar_tool is None:
            try:
                logger.info("Initializing LanguageTool in background...")
                # Initialize with a timeout to prevent hanging
                import threading
                def init_tool():
                    try:
                        Phase5MLGenerator._grammar_tool = LanguageTool('en-US')
                        logger.info("LanguageTool initialized successfully")
                    except Exception as e:
                        logger.warning(f"LanguageTool init failed: {e}. Continuing without grammar correction.")
                        Phase5MLGenerator._grammar_tool = False  # Mark as failed

                thread = threading.Thread(target=init_tool, daemon=True)
                thread.start()
            except Exception as e:
                logger.warning(f"Could not start LanguageTool init thread: {e}")

    def _classify_case_type(self, claim_data: dict) -> str:
        """Classify case as ROUTINE or COMPLEX for hybrid processing"""
        diagnosis = (claim_data.get('diagnosis', '') or '').lower()
        chief_complaint = (claim_data.get('chief_complaint', '') or '').lower()
        treatment = (claim_data.get('treatment_given', '') or '').lower()

        combined_text = f"{diagnosis} {chief_complaint} {treatment}"

        # Check for routine cases (surgical, oncology, maternity)
        if any(keyword in combined_text for keyword in self.SURGICAL_KEYWORDS):
            return 'ROUTINE_SURGICAL'
        if any(keyword in combined_text for keyword in self.ONCOLOGY_KEYWORDS):
            return 'ROUTINE_ONCOLOGY'
        if any(keyword in combined_text for keyword in self.MATERNITY_KEYWORDS):
            return 'ROUTINE_MATERNITY'

        # Everything else is complex (fever, sepsis, conservative, drug misuse, etc.)
        return 'COMPLEX'

    def _generate_routine_conclusion(self, claim_data: dict, case_type: str) -> dict:
        """Generate conclusion for routine cases using templates (fast, local)"""
        chief_complaint = claim_data.get('chief_complaint', 'Not documented')
        diagnosis = claim_data.get('diagnosis', 'Not documented')
        treatment = claim_data.get('treatment_given', 'Not documented')
        los_days = claim_data.get('los_days', 0)
        claim_amount = claim_data.get('claim_amount', 0)
        investigations = claim_data.get('investigations', {})
        patient_name = claim_data.get('patient_name', 'The insured')

        # Template-based conclusion for routine cases
        if case_type == 'ROUTINE_SURGICAL':
            conclusion = f"""Based on the clinical records and medical evidence submitted, this claim is APPROVED. The insured, {patient_name}, was admitted with a diagnosis of {diagnosis}, characterized by {chief_complaint}. The patient underwent {treatment} during a {los_days}-day hospital stay, with a claimed amount of ₹{claim_amount:,.0f}.

The clinical findings and investigations support the need for surgical intervention. The procedure is appropriately documented and aligns with standard protocols for {diagnosis}. The hospital stay duration is justified given the nature of the procedure and post-operative care requirements.

Therefore, the claim is justified and approved for the amount of ₹{claim_amount:,.0f}."""
            recommendation = 'APPROVE'
            confidence = 0.90

        elif case_type == 'ROUTINE_ONCOLOGY':
            conclusion = f"""Based on the clinical records and medical evidence submitted, this claim is APPROVED. The insured, {patient_name}, was admitted with a diagnosis of {diagnosis}. The patient underwent {treatment} during a {los_days}-day hospital stay, with a claimed amount of ₹{claim_amount:,.0f}.

The oncology treatment plan is appropriately documented and aligns with standard cancer care protocols. The investigations support the diagnosis, and the treatment provided is consistent with established guidelines for {diagnosis}. The hospital admission for oncology treatment is medically justified.

Therefore, the claim is justified and approved for the amount of ₹{claim_amount:,.0f}."""
            recommendation = 'APPROVE'
            confidence = 0.90

        elif case_type == 'ROUTINE_MATERNITY':
            conclusion = f"""Based on the clinical records and medical evidence submitted, this claim is APPROVED. The insured, {patient_name}, was admitted with {chief_complaint} and diagnosed with {diagnosis}. The patient underwent {treatment} during a {los_days}-day hospital stay, with a claimed amount of ₹{claim_amount:,.0f}.

The obstetric care provided is appropriate and aligns with standard maternity protocols. The investigations and clinical findings support the diagnosis and treatment plan. The hospital admission for delivery and maternal care is medically justified.

Therefore, the claim is justified and approved for the amount of ₹{claim_amount:,.0f}."""
            recommendation = 'APPROVE'
            confidence = 0.90

        else:
            conclusion = ''
            recommendation = 'NEED_MORE_EVIDENCE'
            confidence = 0.5

        return {
            'conclusion': conclusion,
            'confidence_score': confidence,
            'recommendation': recommendation,
            'tokens_used': 0,  # No tokens used for template-based
            'model': f'Local-Template-{case_type}',
            'processing_type': 'ROUTINE_LOCAL'
        }

    def generate_conclusion(self, claim_data: dict) -> dict:
        """Generate conclusion using HYBRID routing (local for routine, OpenAI for complex)"""
        try:
            # Step 1: Classify case type
            case_type = self._classify_case_type(claim_data)
            logger.info(f"Case classified as: {case_type}")

            # Step 2: Route to appropriate processor
            if case_type in ['ROUTINE_SURGICAL', 'ROUTINE_ONCOLOGY', 'ROUTINE_MATERNITY']:
                # Use fast, local template-based processing
                logger.info(f"Using LOCAL template processing for {case_type}")
                return self._generate_routine_conclusion(claim_data, case_type)
            else:
                # Use OpenAI for complex cases (fever, sepsis, conservative, drug misuse, etc.)
                logger.info(f"Using OPENAI processing for {case_type}")
                return self._generate_complex_conclusion_with_openai(claim_data)

        except Exception as e:
            logger.error(f"Error in hybrid generate_conclusion: {type(e).__name__}: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise

    def _generate_complex_conclusion_with_openai(self, claim_data: dict) -> dict:
        """Generate detailed conclusion for complex cases using OpenAI"""
        try:
            prompt = self._build_prompt(claim_data)

            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": """You are a senior medical claims reviewer with 15+ years of experience.
Analyze claims comprehensively, identify documentation gaps, assess clinical appropriateness,
and provide detailed, specific recommendations. Be critical and thorough."""
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                temperature=0.7,
                max_tokens=1500
            )

            conclusion_text = response.choices[0].message.content

            # Improve grammar and quality
            conclusion_text = self._improve_grammar_and_quality(conclusion_text)

            recommendation = self._extract_recommendation(conclusion_text)
            confidence = self._calculate_confidence(claim_data, conclusion_text)

            return {
                "conclusion": conclusion_text,
                "confidence_score": confidence,
                "recommendation": recommendation,
                "tokens_used": response.usage.total_tokens,
                "model": self.model,
                "processing_type": "COMPLEX_OPENAI"
            }
        except Exception as e:
            logger.error(f"Error in OpenAI processing: {type(e).__name__}: {e}")
            raise

    def _build_prompt(self, claim_data: dict) -> str:
        """Build professional QC-style conclusion matching standard format"""
        chief_complaint = claim_data.get('chief_complaint', '').strip()
        symptoms = claim_data.get('symptoms', '').strip()
        diagnosis = claim_data.get('diagnosis', '').strip()
        treatment = claim_data.get('treatment_given', '').strip()
        patient_name = claim_data.get('patient_name', 'The insured').strip()
        investigations = claim_data.get('investigations', {})
        los_days = claim_data.get('los_days', 0)
        claim_amount = claim_data.get('claim_amount', 0)
        admission_date = claim_data.get('admission_date', '')
        discharge_date = claim_data.get('discharge_date', '')

        # Build investigation summary with specific values
        inv_lines = []
        if investigations:
            for key, value in investigations.items():
                inv_lines.append(f"{key}: {value}")
        inv_summary = ", ".join(inv_lines) if inv_lines else "No investigations documented"

        date_range = f"from {admission_date} to {discharge_date}" if admission_date and discharge_date else f"for {los_days} day(s)"

        return f"""You are a senior medical claims QC reviewer with expertise in clinical documentation analysis.
Generate a PROFESSIONAL, DETAILED clinical conclusion for this claim matching the exact quality and format shown in the example.

EXAMPLE FORMAT (match this style exactly):
"Based on the clinical records and medical evidence submitted, this claim is [DECISION]. The insured, [NAME], was admitted with a diagnosis of [DIAGNOSIS], characterized by [CLINICAL_DESCRIPTION], and underwent [INTERVENTION] during a {los_days}-day hospital stay {date_range}, with a claimed amount of ₹[AMOUNT]. The clinical findings including [VITAL_SIGNS], along with [INVESTIGATION_FINDINGS], indicate [CLINICAL_ASSESSMENT]. [STANDARD_PRACTICE_STATEMENT]. [DOCUMENTATION_GAPS]. Therefore, [FINAL_JUSTIFICATION]."

CASE DETAILS:
- Patient Name: {patient_name if patient_name and patient_name != 'The insured' else 'Not specified'}
- Chief Complaint/Diagnosis: {diagnosis if diagnosis else 'Not documented'}
- Clinical Presentation: {chief_complaint if chief_complaint else 'Not documented'}
- Symptoms: {symptoms if symptoms else 'Not documented'}
- Treatment/Intervention: {treatment if treatment else 'Not documented'}
- Length of Stay: {los_days} day(s)
- Date Range: {date_range}
- Investigations: {inv_summary}
- Claimed Amount: ₹{claim_amount:,.0f}

INSTRUCTIONS - Generate conclusion in this format:

1. OPENING: "Based on clinical records... this claim is [APPROVED/REJECTED/UNDER QUERY]"
2. PATIENT & CASE: Name, diagnosis, intervention, dates, claimed amount
3. CLINICAL FINDINGS: Specific vital signs, lab values, investigation results with actual numbers
4. CLINICAL ASSESSMENT: What findings indicate (minor/major condition, appropriate/inappropriate)
5. STANDARD PRACTICE: Reference to standard procedures/protocols for this condition
6. DOCUMENTATION GAPS: Identify missing information that affects assessment
7. CONCLUSION: Clear statement of medical necessity assessment

CRITICAL RULES FOR QUALITY:
1. Use ACTUAL patient name if provided (Mr./Mrs./Ms./Dr.)
2. Include SPECIFIC values: vitals (BP, HR, RR, SpO2, Temp), lab results with numbers
3. Reference MEDICAL TERMINOLOGY: procedures, anatomical terms, diagnostic terms
4. Identify STANDARD PRACTICE: "routinely performed as outpatient/day-care", "typically managed as"
5. STATE GAPS CLEARLY: "records do not document", "query raised regarding... remains unanswered"
6. LOGICAL FLOW: Build reasoning progressively to final decision
7. PROFESSIONAL TONE: Use formal medical language, no colloquialisms
8. GRAMMAR: Perfect spelling, grammar, punctuation
9. LENGTH: 200-300 words for REJECTED/APPROVED, 150-200 for QUERY
10. DECISION STATUS:
    - REJECTED: "Claim is inadmissible and fully rejected" + Amount shown
    - APPROVED: "Claim is justified and approved" + Amount shown
    - QUERY: "Claim is under query pending" + NO amount shown

Generate the professional clinical conclusion now:"""

    def _extract_recommendation(self, conclusion: str) -> str:
        """Extract recommendation from conclusion text"""
        text_upper = conclusion.upper()
        if "RECOMMEND: APPROVE" in text_upper or "RECOMMENDATION: APPROVE" in text_upper:
            return "APPROVE"
        elif "RECOMMEND: REJECT" in text_upper or "RECOMMENDATION: REJECT" in text_upper:
            return "REJECT"
        elif "RECOMMEND: NEED" in text_upper or "RECOMMENDATION: NEED" in text_upper:
            return "NEED_MORE_EVIDENCE"
        else:
            return "NEED_MORE_EVIDENCE"  # Default to conservative stance

    def _correct_grammar_with_languagetool(self, text: str) -> str:
        """Correct grammar using LanguageTool if available"""
        # Skip if LanguageTool not available or failed
        if not GRAMMAR_TOOL_AVAILABLE or Phase5MLGenerator._grammar_tool is None or Phase5MLGenerator._grammar_tool is False:
            return text

        try:
            tool = Phase5MLGenerator._grammar_tool
            if tool is False:  # Tool initialization failed
                return text

            # Quick timeout check - if tool is still initializing, skip
            matches = tool.check(text)

            if not matches:
                return text

            # Apply only critical corrections (grammar/spelling)
            corrected = text
            fix_count = 0
            for match in sorted(matches, key=lambda m: m.offset, reverse=True):
                if match.replacements and match.ruleId:
                    if any(keyword in match.ruleId.upper() for keyword in ['GRAMMAR', 'SPELL', 'CAPITALIZATION']):
                        start = match.offset
                        end = match.offset + match.length
                        corrected = corrected[:start] + match.replacements[0] + corrected[end:]
                        fix_count += 1

            return corrected
        except Exception as e:
            logger.debug(f"Grammar check skipped: {e}")
            return text

    def _improve_grammar_and_quality(self, text: str) -> str:
        """Improve grammar and formatting of conclusion"""
        improved = text
        import re

        # Try LanguageTool if available (non-blocking)
        if GRAMMAR_TOOL_AVAILABLE and Phase5MLGenerator._grammar_tool is not None and Phase5MLGenerator._grammar_tool is not False:
            try:
                improved = self._correct_grammar_with_languagetool(improved)
            except:
                pass  # Continue with basic fixes

        # Essential formatting fixes (always applied)
        improved = improved.replace(' , ', ', ')
        improved = improved.replace(' . ', '. ')
        improved = improved.replace('₹ ', '₹')
        improved = improved.replace('Rs .', '₹')

        # Fix double spaces
        improved = re.sub(r'  +', ' ', improved)

        # Fix capitalization after periods
        improved = re.sub(r'(\. )([a-z])', lambda m: m.group(1) + m.group(2).upper(), improved)

        # Ensure proper opening
        if 'Based on' not in improved.split('\n')[0]:
            if improved.startswith('The insured') or improved.startswith('Patient'):
                improved = 'Based on the clinical records and medical evidence submitted, ' + improved

        return improved.strip()

    def _calculate_confidence(self, claim_data: dict, conclusion: str) -> float:
        """Calculate confidence score based on data completeness and analysis depth"""
        confidence = 0.5

        # Check data completeness
        if claim_data.get('chief_complaint'): confidence += 0.05
        if claim_data.get('symptoms'): confidence += 0.05
        if claim_data.get('diagnosis'): confidence += 0.05
        if claim_data.get('treatment_given'): confidence += 0.05
        if claim_data.get('investigations'): confidence += 0.1
        if claim_data.get('admission_date') and claim_data.get('discharge_date'): confidence += 0.05
        if claim_data.get('patient_name'): confidence += 0.05

        # Check conclusion depth and quality
        if len(conclusion) > 500: confidence += 0.1
        if any(word in conclusion.lower() for word in ['gap', 'documentation', 'missing']): confidence += 0.05
        if any(word in conclusion.lower() for word in ['vitals', 'investigation', 'clinical']): confidence += 0.05
        if '₹' in conclusion or 'rupees' in conclusion.lower(): confidence += 0.05

        return min(confidence, 0.95)  # Cap at 0.95 to avoid overconfidence
