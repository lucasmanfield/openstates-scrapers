import requests.exceptions
from billy.scrape.votes import VoteScraper, Vote
from billy.scrape.utils import convert_pdf
import datetime
import lxml
import os
import re


date_re = r".*(?P<date>(MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY),\s\w+\s\d{1,2},\s\d{4}).*"
chamber_re = r".*JOURNAL OF THE ((HOUSE)|(SENATE)).*\d+.*DAY.*"


class NDVoteScraper(VoteScraper):
    jurisdiction = 'nd'

    def lxmlize(self, url):
        page = self.urlopen(url)
        page = lxml.html.fromstring(page)
        page.make_links_absolute(url)
        return page

    def scrape(self, chamber, session):
        chamber_name = 'house' if chamber == 'lower' else 'senate'
        session_slug = {
                '62': '62-2011',
                '63': '63-2013',
                '64': '64-2015'
                }[session]

        # Open the index page of the session's Registers, and open each
        url = "http://www.legis.nd.gov/assembly/%s/journals/%s-journal.html" % (
            session_slug, chamber_name)
        page = self.lxmlize(url)
        pdfs = page.xpath("//a[contains(@href, '.pdf')]")
        for pdf in pdfs:

            # Initialize information about the vote parsing
            results = {}
            in_motion = False
            cur_vote = None
            in_vote = False
            cur_motion = ""

            # Determine which URLs the information was pulled from
            pdf_url = pdf.attrib['href']

            try:
                (path, response) = self.urlretrieve(pdf_url)
            except requests.exceptions.ConnectionError:
                continue

            # Convert the PDF to text
            data = convert_pdf(path, type='text')
            os.unlink(path)

            # Determine the date of the document
            date = re.findall(date_re, data)
            if date:
                date = date[0][0]
                cur_date = datetime.datetime.strptime(date, "%A, %B %d, %Y")
            else:
                # If no date is found anywhere, do not process the document
                self.warning("No date was found for the document; skipping.")
                continue

            # Check each line of the text for motion and vote information
            lines = data.splitlines()
            for line in lines:

                # Ensure that motion and vote capturing are not _both_ active
                if in_motion and in_vote:
                    raise AssertionError(
                            "Scraper should not be simultaneously processing " +
                            "motion name and votes, as it is for this motion: " +
                            cur_motion
                            )

                # Ignore lines with no information
                if re.search(chamber_re, line) or \
                        re.search(date_re, line) or \
                        line.strip() == "":
                    pass

                # Capture motion names, which are found after ROLL CALL headers
                elif line.strip() == "ROLL CALL":
                    in_motion = True

                elif in_motion:
                    if cur_motion == "":
                        cur_motion = line.strip()
                    else:
                        cur_motion = cur_motion + " " + line.strip()

                    # ABSENT AND NOT VOTING marks the end of each motion name
                    # In this case, prepare to capture votes
                    if line.strip().endswith("VOTING") or \
                            line.strip().endswith("VOTING."):
                        in_motion = False
                        in_vote = True

                elif in_vote:
                    # Ignore appointments and confirmations
                    if "The Senate advises and consents to the appointment" \
                            in line:
                        in_vote = False
                        cur_vote = None
                        results = {}
                        cur_motion = ""

                    # If votes are being processed, record the voting members
                    elif ":" in line:
                        cur_vote, who = line.split(":", 1)
                        who = [x.strip() for x in who.split(';')]
                        results[cur_vote] = who
                    
                    elif cur_vote is not None and \
                            not any(x in line.lower() for x in
                            ['passed', 'adopted', 'sustained', 'prevailed', 'lost', 'failed']):
                        who = [x.strip() for x in line.split(";")]
                        # print cur_vote
                        results[cur_vote].extend(who)

                    # At the conclusion of a vote, save its data
                    elif any(x in line.lower() for x in
                            ['passed', 'adopted', 'sustained', 'prevailed', 'lost', 'failed']):

                        in_vote = False
                        cur_vote = None

                        # Throw a warning if impropper informaiton found
                        bills = re.findall(r"(?i)(H|S|J)(C?)(B|R|M) (\d+)", line)
                        if bills == [] or cur_motion.strip() == "":
                            results = {}
                            cur_motion = ""
                            self.warning(
                                    "No motion or bill name found: " +
                                    "motion name: " + cur_motion + "; " +
                                    "decision text: " + line.strip()
                                    )
                            continue

                        if "YEAS:" in cur_motion or "NAYS:" in cur_motion:
                            raise AssertionError(
                                    "Vote data found in motion name: " +
                                    cur_motion
                                    )

                        cur_bill_id = "%s%s%s %s" % (bills[-1])

                        # Use the collected results to determine who voted how
                        keys = {
                            "YEAS": "yes",
                            "NAYS": "no",
                            "ABSENT AND NOT VOTING": "other"
                        }
                        res = {}
                        for key in keys:
                            if key in results:
                                res[keys[key]] = filter(lambda a: a != "",
                                                        results[key])
                            else:
                                res[keys[key]] = []

                        # Count the number of members voting each way
                        yes, no, other = len(
                                res['yes']), \
                                len(res['no']), \
                                len(res['other']
                                )
                        chambers = {
                            "H": "lower",
                            "S": "upper",
                            "J": "joint"
                        }

                        # Identify the source chamber for the bill
                        try:
                            bc = chambers[cur_bill_id[0]]
                        except KeyError:
                            bc = 'other'

                        # Determine whether or not the vote passed
                        if "over the governor's veto" in cur_motion.lower():
                            passed = (yes * 2 > no * 3)
                        else:
                            passed = (yes > no)

                        # Create a Vote object based on the scraped information
                        vote = Vote(chamber,
                                    cur_date,
                                    cur_motion,
                                    passed,
                                    yes,
                                    no,
                                    other,
                                    session=session,
                                    bill_id=cur_bill_id,
                                    bill_chamber=bc)

                        vote.add_source(pdf_url)
                        vote.add_source(url)

                        # For each category of voting members,
                        # add the individuals to the Vote object
                        for key in res:
                            obj = getattr(vote, key)
                            for person in res[key]:
                                obj(person)

                        print("***")
                        self.save_vote(vote)

                        # With the vote successfully processed,
                        # wipe its data and continue to the next one
                        results = {}
                        cur_motion = ""
