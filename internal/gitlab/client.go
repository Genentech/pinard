package gitlab

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
)

type Client struct {
	Host  string
	Token string
	HTTP  *http.Client
}

func NewClient(host, token string) *Client {
	return &Client{
		Host:  host,
		Token: token,
		HTTP: &http.Client{
			Timeout: 30 * time.Second,
		},
	}
}

func (c *Client) apiURL(path string) string {
	return fmt.Sprintf("https://%s/api/v4/%s", c.Host, path)
}

func (c *Client) encodedRepo(repo string) string {
	return url.PathEscape(repo)
}

func (c *Client) get(path string, result any) error {
	req, err := http.NewRequest("GET", c.apiURL(path), nil)
	if err != nil {
		return err
	}
	req.Header.Set("PRIVATE-TOKEN", c.Token)

	resp, err := c.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("GitLab API %s: %d %s", path, resp.StatusCode, string(body))
	}

	return json.NewDecoder(resp.Body).Decode(result)
}

func (c *Client) put(path string, params map[string]string) error {
	form := url.Values{}
	for k, v := range params {
		form.Set(k, v)
	}

	req, err := http.NewRequest("PUT", c.apiURL(path), strings.NewReader(form.Encode()))
	if err != nil {
		return err
	}
	req.Header.Set("PRIVATE-TOKEN", c.Token)
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")

	resp, err := c.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("GitLab API PUT %s: %d %s", path, resp.StatusCode, string(body))
	}
	return nil
}

func (c *Client) post(path string, params map[string]string) ([]byte, error) {
	form := url.Values{}
	for k, v := range params {
		form.Set(k, v)
	}

	req, err := http.NewRequest("POST", c.apiURL(path), strings.NewReader(form.Encode()))
	if err != nil {
		return nil, err
	}
	req.Header.Set("PRIVATE-TOKEN", c.Token)
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")

	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return body, fmt.Errorf("GitLab API POST %s: %d %s", path, resp.StatusCode, string(body))
	}
	return body, nil
}

func (c *Client) GetMR(repo string, iid int) (*MergeRequest, error) {
	var mr MergeRequest
	err := c.get(fmt.Sprintf("projects/%s/merge_requests/%d", c.encodedRepo(repo), iid), &mr)
	return &mr, err
}

func (c *Client) ListMRNotes(repo string, iid int) ([]Note, error) {
	var notes []Note
	err := c.get(fmt.Sprintf("projects/%s/merge_requests/%d/notes?per_page=100&sort=asc", c.encodedRepo(repo), iid), &notes)
	return notes, err
}

func (c *Client) ListMRPipelines(repo string, iid int) ([]Pipeline, error) {
	var pipelines []Pipeline
	err := c.get(fmt.Sprintf("projects/%s/merge_requests/%d/pipelines", c.encodedRepo(repo), iid), &pipelines)
	return pipelines, err
}

func (c *Client) ListPipelinesByCommit(repo string, sha string) ([]Pipeline, error) {
	var pipelines []Pipeline
	err := c.get(fmt.Sprintf("projects/%s/pipelines?sha=%s", c.encodedRepo(repo), sha), &pipelines)
	return pipelines, err
}

func (c *Client) ListMRsByAssignee(repo string, assignee string) ([]MergeRequest, error) {
	var mrs []MergeRequest
	path := fmt.Sprintf("projects/%s/merge_requests?assignee_username=%s&state=opened&per_page=100",
		c.encodedRepo(repo), url.QueryEscape(assignee))
	err := c.get(path, &mrs)
	return mrs, err
}

func (c *Client) MergeMR(repo string, iid int) error {
	return c.put(fmt.Sprintf("projects/%s/merge_requests/%d/merge", c.encodedRepo(repo), iid), nil)
}

// MergeMRWithMessage merges an MR and sets an explicit merge commit message.
func (c *Client) MergeMRWithMessage(repo string, iid int, mergeCommitMessage string) error {
	params := map[string]string{}
	if mergeCommitMessage != "" {
		params["merge_commit_message"] = mergeCommitMessage
	}
	return c.put(fmt.Sprintf("projects/%s/merge_requests/%d/merge", c.encodedRepo(repo), iid), params)
}

func (c *Client) ListIssues(repo string, assignee string) ([]Issue, error) {
	var issues []Issue
	path := fmt.Sprintf("projects/%s/issues?assignee_username=%s&state=opened&per_page=100",
		c.encodedRepo(repo), url.QueryEscape(assignee))
	err := c.get(path, &issues)
	return issues, err
}

func (c *Client) ListIssuesByLabel(repo string, label string) ([]Issue, error) {
	var issues []Issue
	path := fmt.Sprintf("projects/%s/issues?labels=%s&state=opened&per_page=100",
		c.encodedRepo(repo), url.QueryEscape(label))
	err := c.get(path, &issues)
	return issues, err
}

func (c *Client) GetIssue(repo string, iid int) (*Issue, error) {
	var issue Issue
	err := c.get(fmt.Sprintf("projects/%s/issues/%d", c.encodedRepo(repo), iid), &issue)
	return &issue, err
}

func (c *Client) ListIssueNotes(repo string, iid int) ([]Note, error) {
	var notes []Note
	err := c.get(fmt.Sprintf("projects/%s/issues/%d/notes?sort=asc&per_page=50", c.encodedRepo(repo), iid), &notes)
	return notes, err
}

func (c *Client) GetMRApprovals(repo string, iid int) (*Approvals, error) {
	var approvals Approvals
	err := c.get(fmt.Sprintf("projects/%s/merge_requests/%d/approvals", c.encodedRepo(repo), iid), &approvals)
	return &approvals, err
}

func (c *Client) ListMRDiscussions(repo string, iid int) ([]Discussion, error) {
	var discussions []Discussion
	err := c.get(fmt.Sprintf("projects/%s/merge_requests/%d/discussions", c.encodedRepo(repo), iid), &discussions)
	return discussions, err
}

func (c *Client) CreateMR(repo string, params map[string]string) ([]byte, error) {
	return c.post(fmt.Sprintf("projects/%s/merge_requests", c.encodedRepo(repo)), params)
}

// GetMRChanges returns the list of changed file paths for an MR (no diff hunks).
func (c *Client) GetMRChanges(repo string, iid int) ([]string, error) {
	var result struct {
		Changes []struct {
			NewPath string `json:"new_path"`
			OldPath string `json:"old_path"`
			DeletedFile bool   `json:"deleted_file"`
		} `json:"changes"`
	}
	err := c.get(fmt.Sprintf("projects/%s/merge_requests/%d/changes", c.encodedRepo(repo), iid), &result)
	if err != nil {
		return nil, err
	}
	seen := make(map[string]bool)
	var paths []string
	for _, ch := range result.Changes {
		path := ch.NewPath
		if path == "" {
			path = ch.OldPath
		}
		if path != "" && !seen[path] {
			seen[path] = true
			paths = append(paths, path)
		}
	}
	return paths, nil
}

// GetMRClosingIssues returns the issues that the MR closes via the GitLab API.
func (c *Client) GetMRClosingIssues(repo string, iid int) ([]Issue, error) {
	var issues []Issue
	err := c.get(fmt.Sprintf("projects/%s/merge_requests/%d/closes_issues?per_page=20", c.encodedRepo(repo), iid), &issues)
	return issues, err
}

var _closesRE = regexp.MustCompile(`(?i)(?:closes?|fixes?|resolves?)\s+#(\d+)`)

// ParseClosesN extracts issue IIDs referenced by "Closes #N" patterns in an MR description.
func ParseClosesN(description string) []int {
	matches := _closesRE.FindAllStringSubmatch(description, -1)
	seen := make(map[int]bool)
	var iids []int
	for _, m := range matches {
		n, err := strconv.Atoi(m[1])
		if err == nil && !seen[n] {
			seen[n] = true
			iids = append(iids, n)
		}
	}
	return iids
}

func (c *Client) CreateIssue(repo string, params map[string]string) ([]byte, error) {
	return c.post(fmt.Sprintf("projects/%s/issues", c.encodedRepo(repo)), params)
}

func (c *Client) UpdateMR(repo string, iid int, params map[string]string) error {
	return c.put(fmt.Sprintf("projects/%s/merge_requests/%d", c.encodedRepo(repo), iid), params)
}

func (c *Client) UpdateIssue(repo string, iid int, params map[string]string) error {
	return c.put(fmt.Sprintf("projects/%s/issues/%d", c.encodedRepo(repo), iid), params)
}

func (c *Client) PostIssueNote(repo string, iid int, body string) error {
	_, err := c.post(fmt.Sprintf("projects/%s/issues/%d/notes", c.encodedRepo(repo), iid),
		map[string]string{"body": body})
	return err
}

func (c *Client) PostMRNote(repo string, iid int, body string) error {
	_, err := c.post(fmt.Sprintf("projects/%s/merge_requests/%d/notes", c.encodedRepo(repo), iid),
		map[string]string{"body": body})
	return err
}

func (c *Client) PostDiscussionNote(repo string, iid int, discussionID, body string) error {
	_, err := c.post(fmt.Sprintf("projects/%s/merge_requests/%d/discussions/%s/notes",
		c.encodedRepo(repo), iid, discussionID),
		map[string]string{"body": body})
	return err
}

// MRDiscussionPosition describes the diff position for an inline discussion.
type MRDiscussionPosition struct {
	BaseSHA  string
	StartSHA string
	HeadSHA  string
	NewPath  string
	OldPath  string
	NewLine  int
	OldLine  int
}

// PostMRDiscussion opens a new MR discussion (inline when pos is non-nil, top-level otherwise).
// For inline comments the position fields are required by GitLab.
func (c *Client) PostMRDiscussion(repo string, iid int, body string, pos *MRDiscussionPosition) error {
	params := map[string]string{"body": body}
	if pos != nil {
		params["position[position_type]"] = "text"
		params["position[base_sha]"] = pos.BaseSHA
		params["position[start_sha]"] = pos.StartSHA
		params["position[head_sha]"] = pos.HeadSHA
		params["position[new_path]"] = pos.NewPath
		params["position[old_path]"] = pos.OldPath
		if pos.NewLine > 0 {
			params["position[new_line]"] = fmt.Sprintf("%d", pos.NewLine)
		}
		if pos.OldLine > 0 {
			params["position[old_line]"] = fmt.Sprintf("%d", pos.OldLine)
		}
	}
	_, err := c.post(fmt.Sprintf("projects/%s/merge_requests/%d/discussions", c.encodedRepo(repo), iid), params)
	return err
}

// LookupUser resolves a GitLab username to a UserID string. Returns an error when the user is not found.
func (c *Client) LookupUser(username string) (string, error) {
	var users []struct {
		ID       int    `json:"id"`
		Username string `json:"username"`
	}
	if err := c.get(fmt.Sprintf("users?username=%s", url.QueryEscape(username)), &users); err != nil {
		return "", err
	}
	for _, u := range users {
		if u.Username == username {
			return u.Username, nil
		}
	}
	if len(users) > 0 {
		return users[0].Username, nil
	}
	return "", fmt.Errorf("gitlab: user %q not found", username)
}

// GetUserNumericID resolves a GitLab username to its numeric ID. Returns an error when the user is not found.
func (c *Client) GetUserNumericID(username string) (int, error) {
	var users []struct {
		ID       int    `json:"id"`
		Username string `json:"username"`
	}
	if err := c.get(fmt.Sprintf("users?username=%s", url.QueryEscape(username)), &users); err != nil {
		return 0, err
	}
	for _, u := range users {
		if u.Username == username {
			return u.ID, nil
		}
	}
	if len(users) > 0 {
		return users[0].ID, nil
	}
	return 0, fmt.Errorf("gitlab: user %q not found", username)
}

// GetProject returns the project metadata (including default_branch).
func (c *Client) GetProject(repo string) (map[string]interface{}, error) {
	var result map[string]interface{}
	if err := c.get(fmt.Sprintf("projects/%s", c.encodedRepo(repo)), &result); err != nil {
		return nil, err
	}
	return result, nil
}

// CreateIssueLink creates a link between two issues in the same or different projects.
func (c *Client) CreateIssueLink(repo string, iid int, targetRepo string, targetIID int, linkType string) error {
	params := map[string]string{
		"target_project_id": targetRepo,
		"target_issue_iid":  fmt.Sprintf("%d", targetIID),
		"link_type":         linkType,
	}
	_, err := c.post(fmt.Sprintf("projects/%s/issues/%d/links", c.encodedRepo(repo), iid), params)
	return err
}

func (c *Client) ApproveMR(repo string, iid int) error {
	_, err := c.post(fmt.Sprintf("projects/%s/merge_requests/%d/approve", c.encodedRepo(repo), iid), nil)
	return err
}

// ListAllOpenMRs returns all open MRs in the project.
func (c *Client) ListAllOpenMRs(repo string) ([]MergeRequest, error) {
	var mrs []MergeRequest
	path := fmt.Sprintf("projects/%s/merge_requests?state=opened&per_page=100", c.encodedRepo(repo))
	err := c.get(path, &mrs)
	return mrs, err
}
