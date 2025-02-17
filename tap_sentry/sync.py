import os
import json
import asyncio
import urllib
from pathlib import Path
from itertools import repeat

from singer import Schema
from urllib.parse import urljoin

import pytz
import singer
import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
import pendulum
from singer.bookmarks import write_bookmark, get_bookmark
from pendulum import datetime, period

import dateutil.parser
from dateutil import tz

LOGGER = singer.get_logger()
class SentryAuthentication(requests.auth.AuthBase):
    def __init__(self, api_token: str):
        self.api_token = api_token

    def __call__(self, req):
        req.headers.update({"Authorization": " Bearer " + self.api_token})

        return req


class SentryClient:
    def __init__(self, auth: SentryAuthentication, url="https://sentry.io/api/0/"):
        self._base_url = url
        self._auth = auth
        self._session = None

    @property
    def session(self):
        if not self._session:
            self._session = requests.Session()
            self._session.auth = self._auth
            self._session.headers.update({"Accept": "application/json"})

            retries = Retry(total=5,
                            backoff_factor=0.1,
                            status_forcelist=[ 429, 500, 502, 503, 504 ],
                            respect_retry_after_header=True)

            self._session.mount('https://', HTTPAdapter(max_retries=retries))

        return self._session

    def _get(self, path, params=None):
        url = self._base_url + path
        response = self.session.get(url, params=params)
        response.raise_for_status()

        return response

    def projects(self):
        projects = self._get(f"projects/")
        return projects.json()

    def issues(self, project_id, state):
        bookmark = get_bookmark(state, "issues", "start")
        query = f"projects/rise-people/{project_id}/issues/"
        if bookmark:
            date_filter = urllib.parse.quote("lastSeen:>=" + bookmark)
            query += "?query=" + date_filter
        response = self._get(query)
        issues = response.json()
        url= response.url
        while (response.links is not None and response.links.__len__() >0  and response.links['next']['results'] == 'true'):
            url = response.links['next']['url']
            response = self.session.get(url)
            issues += response.json()
        return issues

    def activity(self, state):
        bookmark = dateutil.parser.parse(get_bookmark(state, 'activity', 'start')).replace(tzinfo=tz.gettz('UTC'))
        response = self._get("organizations/rise-people/activity/")
        activities = self._filter_activities(response.json(), bookmark)

        if not activities:
            return activities

        url = response.url
        while (response.links is not None and response.links.__len__() >0  and response.links['next']['results'] == 'true'):
            url = response.links['next']['url']
            response = self.session.get(url)
            new_activities = self._filter_activities(response.json(), bookmark)
            if not new_activities:
                break
            else:
                activities += new_activities

        return activities

    def _filter_activities(self, activities, bookmark):
        return [activity for activity in activities if dateutil.parser.parse(activity['dateCreated']) >= bookmark]

    def events(self, project_id, state):
        try:
            bookmark = get_bookmark(state, "events", "start")
            query = f"/organizations/split-software/events/?project={project_id}"
            if bookmark:
                query += "&start=" + urllib.parse.quote(bookmark) + "&utc=true" + '&end=' + urllib.parse.quote(singer.utils.strftime(singer.utils.now()))
            response = self._get(query)
            events = response.json()
            url= response.url
            while (response.links is not None and response.links.__len__() >0  and response.links['next']['results'] == 'true'):
                url = response.links['next']['url']
                response = self.session.get(url)
                events += response.json()
            return events
        except:
            return None
        

    def teams(self, state):
        response = self._get(f"organizations/rise-people/teams/")
        teams = response.json()
        extraction_time = singer.utils.now()
        while (response.links is not None and response.links.__len__() >0  and  response.links['next']['results'] == 'true'):
            url = response.links['next']['url']
            response = self.session.get(url)
            teams += response.json()
        return teams

    def users(self, state):
        response = self._get(f"organizations/rise-people/users/")
        users = response.json()
        return users


class SentrySync:
    def __init__(self, client: SentryClient, state={}):
        self._client = client
        self._state = state
        self.projects = self.client.projects()

    @property
    def client(self):
        return self._client

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, value):
        singer.write_state(value)
        self._state = value

    def sync(self, stream, schema):
        func = getattr(self, f"sync_{stream}")
        return func(schema)

    async def sync_issues(self, schema, period: pendulum.period = None):
        """Issues per project."""
        stream = "issues"
        loop = asyncio.get_event_loop()
        issues_synced = []

        singer.write_schema(stream, schema.to_dict(), ["id"])
        extraction_time = singer.utils.now()
        if self.projects:
            for project in self.projects:
                issues = await loop.run_in_executor(None, self.client.issues, project['slug'], self.state)
                if (issues):
                    for issue in issues:
                        issues_synced.append(issue['id'])
                        singer.write_record(stream, issue)

        self.state = singer.write_bookmark(self.state, 'issues', 'start', singer.utils.strftime(extraction_time))

        activities = await loop.run_in_executor(None, self.client.activity, self.state)
        if activities:
            issue_activities = [activity for activity in activities if activity['issue']]
            for activity in issue_activities:
                issue = activity['issue']
                if issue['id'] not in issues_synced:
                    issues_synced.append(issue['id'])
                    singer.write_record(stream, issue)

        self.state = singer.write_bookmark(self.state, 'activity', 'start', singer.utils.strftime(extraction_time))

    async def sync_projects(self, schema):
        """Issues per project."""
        stream = "projects"
        loop = asyncio.get_event_loop()
        singer.write_schema('projects', schema.to_dict(), ["id"])
        if self.projects:
            for project in self.projects:
                singer.write_record(stream, project)


    async  def sync_events(self, schema, period: pendulum.period = None):
        """Events per project."""
        stream = "events"
        loop = asyncio.get_event_loop()

        singer.write_schema(stream, schema.to_dict(), ["eventID"])  
        extraction_time = singer.utils.now()
        if self.projects:
            for project in self.projects:
                events = await loop.run_in_executor(None, self.client.events, project['id'], self.state)
                if events:
                    for event in events:
                        singer.write_record(stream, event)
            self.state = singer.write_bookmark(self.state, 'events', 'start', singer.utils.strftime(extraction_time))

    async def sync_users(self, schema):
        "Users in the organization."
        stream = "users"
        loop = asyncio.get_event_loop()
        singer.write_schema(stream, schema.to_dict(), ["id"]) 
        users = await loop.run_in_executor(None, self.client.users, self.state)
        if users:
            for user in users:
                singer.write_record(stream, user)
        #extraction_time = singer.utils.now()
        #self.state = singer.write_bookmark(self.state, 'users', 'dateCreated', singer.utils.strftime(extraction_time))

    async def sync_teams(self, schema):
        "Teams in the organization."
        stream = "teams"
        loop = asyncio.get_event_loop()
        singer.write_schema(stream, schema.to_dict(), ["id"]) 
        teams = await loop.run_in_executor(None, self.client.teams, self.state)
        if teams:
            for team in teams:
                singer.write_record(stream, team)
        #extraction_time = singer.utils.now()
        #self.state = singer.write_bookmark(self.state, 'teams', 'dateCreated', singer.utils.strftime(extraction_time))
