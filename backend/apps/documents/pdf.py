from html import escape
from io import BytesIO

from django.contrib.staticfiles import finders
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .models import WorkflowDecision


NAVY = colors.HexColor("#302F6E")
TEXT = colors.HexColor("#202433")
MUTED = colors.HexColor("#687083")
LINE = colors.HexColor("#E1E6EF")


def _plain_paragraph(value, style):
    return Paragraph(escape(str(value or "")).replace("\n", "<br/>"), style)


def render_memo_pdf(document, *, version=None):
    version = version or document.current_version
    if version.document_id != document.pk:
        raise ValueError("The rendered version must belong to the document.")
    content = version.content
    buffer = BytesIO()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("APGCTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=16,
                              leading=20, textColor=NAVY, alignment=TA_CENTER, spaceAfter=3 * mm))
    styles.add(ParagraphStyle("APGCSection", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=10,
                              leading=13, textColor=NAVY, spaceBefore=4 * mm, spaceAfter=2 * mm))
    styles.add(ParagraphStyle("APGCBody", parent=styles["BodyText"], fontName="Helvetica", fontSize=10,
                              leading=15, textColor=TEXT, spaceAfter=3 * mm))
    styles.add(ParagraphStyle("APGCLabel", parent=styles["BodyText"], fontName="Helvetica-Bold", fontSize=9,
                              leading=12, textColor=MUTED))
    styles.add(ParagraphStyle("APGCValue", parent=styles["BodyText"], fontName="Helvetica", fontSize=9,
                              leading=12, textColor=TEXT))

    def draw_footer(canvas, _doc):
        canvas.saveState()
        canvas.setStrokeColor(LINE)
        canvas.line(18 * mm, 17 * mm, 192 * mm, 17 * mm)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(18 * mm, 11 * mm, f"{document.reference} · APGC Document Management System")
        canvas.drawRightString(192 * mm, 11 * mm, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()

    pdf = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=15 * mm,
        bottomMargin=23 * mm,
        title=f"Internal Memo {document.reference}",
        author="Alliance Power Generation Company",
    )
    story = []
    logo_path = finders.find("apgc/brand/alliance-power-logo.png")
    if logo_path:
        story.append(Image(logo_path, width=68 * mm, height=14 * mm, kind="proportional"))
    story.extend([
        Spacer(1, 5 * mm),
        Paragraph("INTERNAL MEMO", styles["APGCTitle"]),
        Paragraph(escape(document.reference), ParagraphStyle("APGCReference", parent=styles["APGCValue"], alignment=TA_CENTER)),
        Spacer(1, 4 * mm),
    ])

    rows = [
        ("Date", document.memo_date.strftime("%d %B %Y")),
        ("From", document.created_by.get_full_name() or document.created_by.get_username()),
        ("Originating department", document.originating_assignment.department_name_snapshot if document.originating_assignment else "—"),
        ("Originating position", document.originating_assignment.position_title_snapshot if document.originating_assignment else "—"),
        ("To", content.get("to", "")),
        ("Through", content.get("through", "") or "—"),
        ("CC", content.get("cc", "") or "—"),
        ("Classification", document.get_classification_display()),
        ("Subject", content.get("subject", document.title)),
    ]
    info = [[_plain_paragraph(label, styles["APGCLabel"]), _plain_paragraph(value, styles["APGCValue"])] for label, value in rows]
    info_table = Table(info, colWidths=[34 * mm, 140 * mm], hAlign="LEFT")
    info_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F0F0FA")),
        ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
        ("LINEBELOW", (0, 0), (-1, -1), .35, LINE),
    ]))
    story.extend([info_table, Paragraph("Memo", styles["APGCSection"])])
    body = content.get("body", "")
    for paragraph in (part.strip() for part in body.split("\n\n")):
        if paragraph:
            story.append(_plain_paragraph(paragraph, styles["APGCBody"]))

    attachments = list(version.attachments.all())
    story.append(Paragraph("Attachments", styles["APGCSection"]))
    if attachments:
        story.extend(_plain_paragraph(f"• {attachment.original_filename}", styles["APGCValue"]) for attachment in attachments)
    else:
        story.append(_plain_paragraph("No attachments", styles["APGCValue"]))

    approval = WorkflowDecision.objects.filter(
        task__instance__document=document,
        task__submitted_version=version,
        outcome=WorkflowDecision.Outcome.APPROVE,
    ).select_related("actor", "actor_assignment__department", "actor_assignment__position").order_by("-decided_at").first()
    story.append(Paragraph("Approval and finalization", styles["APGCSection"]))
    if approval:
        approved_at = timezone.localtime(approval.decided_at).strftime("%d %B %Y, %H:%M")
        approval_context = ""
        if approval.actor_assignment:
            approval_context = f" ({approval.actor_assignment.department_name_snapshot} / {approval.actor_assignment.position_title_snapshot})"
        story.append(_plain_paragraph(f"Approved by {approval.actor.get_full_name() or approval.actor.get_username()}{approval_context} on {approved_at}.", styles["APGCValue"]))
    finalized_at = timezone.localtime(document.finalized_at).strftime("%d %B %Y, %H:%M") if document.finalized_at else "—"
    finalizer = document.finalized_by.get_full_name() or document.finalized_by.get_username() if document.finalized_by else "—"
    story.append(_plain_paragraph(f"Finalized by {finalizer} on {finalized_at}.", styles["APGCValue"]))

    pdf.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    return buffer.getvalue()
