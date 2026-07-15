from django.urls import path

from .views import (
    JiraCommentPushView,
    JiraConnectionFullView,
    JiraConnectionView,
    JiraIssueDetailView,
    JiraIssueSearchView,
)

urlpatterns = [
    path("connection/", JiraConnectionView.as_view(), name="jira_connection"),
    path("connection/full/", JiraConnectionFullView.as_view(), name="jira_connection_full"),
    path("search/", JiraIssueSearchView.as_view(), name="jira_search"),
    path("issue/<str:issue_key>/", JiraIssueDetailView.as_view(), name="jira_issue_detail"),
    path("comment/", JiraCommentPushView.as_view(), name="jira_comment_push"),
]
