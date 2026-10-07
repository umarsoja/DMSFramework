from django.urls import path

from . import views
from . import related_views

app_name = "documents"

urlpatterns = [
    path("memos/", views.memo_list, name="memo-list"),
    path("memos/new/", views.memo_create, name="memo-create"),
    path("memos/<int:pk>/", views.memo_detail, name="memo-detail"),
    path("memos/<int:pk>/edit/", views.memo_edit, name="memo-edit"),
    path("memos/<int:pk>/related-panel/", related_views.related_documents_panel, name="related-documents-panel"),
    path("memos/<int:pk>/related/", related_views.relate_document, name="memo-relate"),
    path("memos/<int:pk>/related/<int:relationship_id>/remove/", related_views.unrelate_document, name="memo-unrelate"),
    path("memos/<int:pk>/review/", views.review_memo, name="memo-review"),
    path("memos/<int:pk>/finalize/", views.finalize_memo, name="memo-finalize"),
    path("memos/<int:pk>/archive/", views.archive_memo, name="memo-archive"),
    path("memos/<int:pk>/pdf/", views.memo_pdf_view, name="memo-pdf-view"),
    path("memos/<int:pk>/pdf/download/", views.memo_pdf_download, name="memo-pdf-download"),
    path("memos/<int:pk>/attachments/<int:attachment_id>/view/", views.attachment_view, name="attachment-view"),
    path("memos/<int:pk>/attachments/<int:attachment_id>/download/", views.attachment_download, name="attachment-download"),
]
