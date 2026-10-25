# Cubbie privacy — verbatim text extracted from https://cubbie.ai/en-US/privacy

Fetched for design review. Treat as authoritative over any summary.

Privacy Policy · Cubbie
Home
Privacy Policy
Last updated: 2026-10-22
This Privacy Policy describes how Imaginix Inc. ("we", "us", or "the Company") collects, uses, and discloses your information when you use Cubbie (the "Service"), and the choices you have. By using the Service you agree to the collection and use of information in accordance with this policy.
Information We Collect
Account data.
When you sign in, we collect your name, email address and phone number through our authentication provider, Clerk, so we can create and manage your account.
Course content you choose to process.
Cubbie is a Canvas LMS assistant. When you ask it to scan or organize your courses, it reads the course pages, files, and materials you direct it to, only to provide the features you request. We do not sell this content and do not use it to build advertising profiles.
Canvas sign-in data.
To read your courses, Cubbie stores the Canvas session that the Cubbie Chrome extension or our secure browser signs in with, and any school login details you choose to save so it can sign in again. These are encrypted before they are stored.
Conversations and memory.
We keep your conversation history with Cubbie, including the content it read to answer you, and the notes Cubbie remembers about you to personalise later answers.
Connected account content.
If you connect a Google account, Cubbie accesses the mail, files, calendar entries, tasks and contacts described in the table below, only to answer your requests and to surface course-related mail and deadlines to you.
Message content.
If you enable the iMessage channel, we process the messages you send to the Cubbie line and the replies to them, as described under "iMessage Messages" below.
Usage data.
We automatically collect technical information such as your IP address, browser type and version, the pages or features used, and timestamps, to operate and improve the Service.
SMS Verification
When you request an SMS verification code, Cubbie collects and processes the mobile phone number you provide to deliver the requested one-time passcode and authenticate your account. Verification messages are sent only in response to user-initiated requests. Cubbie does not use SMS verification consent to send marketing or promotional messages. No mobile information or SMS opt-in consent information will be shared with third parties or affiliates for marketing or promotional purposes. Information may be processed by our messaging service providers solely to deliver verification messages and operate the service.
Google User Data
Connecting a Google account is optional. When you connect one, Google's consent screen asks once for all of the permissions in the table below, so Cubbie can help with mail, files, calendar, tasks and contacts without asking again. You can review every permission you have granted at
your Google Account permissions page
.
Scope
Category
Why we request it
What we retain
openid
userinfo.email
userinfo.profile
Non-sensitive
Identify the Google account you connect.
The account's email address, name and Google account ID, kept with the connection.
gmail.modify
Restricted
Sync your mailbox so Cubbie can find, summarise and answer questions about course-related mail; draft replies and send them when you ask; and apply labels or mark messages as read when you ask Cubbie to organise your mail. Cubbie cannot permanently delete mail.
Metadata, snippets and body text of synced mail in our database, plus mail content Cubbie reads in your conversation history and memory. Drafts are saved in your own Gmail account.
drive
Restricted
Read, create and edit the Drive files, Google Docs and Sheets you ask Cubbie to work on, including copying, updating or sharing files you already own.
Not synced in the background. Content Cubbie reads to answer you is saved in your conversation history and memory until you delete your account. Files you ask Cubbie to download are kept for up to 30 days.
calendar.readonly
calendar.events
Sensitive
Show your class schedule and free time and, when you ask, add or change assignment deadlines and exam reminders on your calendars.
Not synced in the background. Content Cubbie reads to answer you is saved in your conversation history and memory until you delete your account.
calendar.app.created
Non-sensitive
Create a separate calendar for Cubbie's reminders and manage events on that calendar only.
Not synced in the background. Content Cubbie reads to answer you is saved in your conversation history and memory until you delete your account.
tasks
Sensitive
Read your Google Tasks and add or update coursework to-dos when you ask.
Not synced in the background. Content Cubbie reads to answer you is saved in your conversation history and memory until you delete your account.
contacts
Sensitive
Look up recipient addresses when you draft a message to an instructor or classmate, and add or update a contact when you ask.
Not synced in the background. Content Cubbie reads to answer you is saved in your conversation history and memory until you delete your account.
Some of these scopes are classified by Google as
restricted
or
sensitive
. The protections that apply to them are set out under "Sensitive and Restricted Data" and "Security" below.
Limited Use of Google User Data
Cubbie's use and transfer of information received from Google APIs adheres to the
Google API Services User Data Policy
, including the Limited Use requirements. Specifically:
We use Google user data only to provide or improve user-facing features that are prominent in the Cubbie interface, and only for the purposes you authorised.
We do not sell Google user data, and we do not transfer it to others except to the sub-processors listed below acting on our instructions, to comply with applicable law, or as part of a merger or acquisition after giving notice and obtaining your consent.
We do not use Google user data for advertising, retargeting, personalised advertising, or credit-scoring purposes.
We do not allow humans to read your Google user data, unless you have given explicit consent for specific messages, it is necessary for security purposes such as investigating abuse, it is required to comply with applicable law, or the data has been aggregated and anonymised for internal operations.
iMessage Messages
Cubbie can talk with you over iMessage. When you enable this channel we assign you a phone line operated by Cubbie, and you text an activation phrase to that line from your own phone to complete the binding. Only a number matching the phone number already verified on your account can bind.
We receive only the messages you send to that Cubbie line.
We do not read the Messages app on your phone, and we cannot access your conversations with anyone else.
On this channel we process and store:
Your phone number and the conversation identifier for your binding;
The text and any attached media of the messages you send to Cubbie, and the content of Cubbie's replies;
Delivery status and timestamps, used for de-duplication, retries, and troubleshooting.
Messages you send are passed to the model providers listed below to generate a reply, and are carried by the messaging provider listed below. You can unbind at any time, after which messages from your number are no longer passed to Cubbie's assistant or the model providers. Messages sent to the line after you unbind are still logged with their delivery status.
AI Processing and Model Training
Cubbie is an AI assistant: content you ask it to work with, including mail and course material, is sent to the large language model providers listed below to generate the answers and summaries you request. Those providers act as our processors and are not permitted to use your content to train their models.
We do not use Google user data to develop, improve, or train generalised AI or machine-learning models.
Sensitive and Restricted Data
Mail content, files, calendar entries and contacts are treated as sensitive data and receive protections beyond our baseline:
Minimisation.
Only Gmail is synced in the background, because incremental sync requires it. Drive, Docs, Sheets, Calendar, Tasks and Contacts are read only when Cubbie needs them for a request; what it reads is kept in your conversation history and not synced into a separate copy of your account.
Channel isolation.
The iMessage channel receives only messages addressed to the Cubbie line. We do not read the Messages app on your device and cannot access your conversations with anyone else.
Purpose limitation.
This data is never used for advertising, advertising profiles, resale, or model training.
Access control.
Google API calls made with a stored credential, token refreshes and revocations are recorded in an access log. The log is kept when a connection is removed and deleted with your account.
Isolation.
Credentials are stored separately from content, encrypted under a key held in our secrets manager rather than in the database or application code.
Independent assessment.
Our handling of restricted scopes is subject to an annual third-party security assessment (CASA) as required by Google.
How We Use Your Information
To provide, maintain, and improve the Service;
To create and manage your account and authenticate you;
To respond to your requests and provide customer support;
To analyze usage trends and the effectiveness of features and campaigns;
To detect, prevent, and address technical issues and abuse.
Sharing Your Information
We share personal data only with service providers who process it on our behalf, with affiliates, in connection with a business transfer, or where required by law. These providers are bound by contract to protect your data, to process it only on our instructions, and may not use it for their own purposes.
Sub-processors.
The providers below process personal data on our behalf today. When we add, replace, or retire one, we update this list and record the change in the log at the end of this policy; on retirement we instruct that provider to delete all data and credentials it holds for us.
Provider
Data processed
Purpose
Status
Google Cloud (GCP)
All data we host, including account records, conversations and synced mail.
Application hosting, database and object storage in the United States.
Current
Google Cloud Vertex AI
Voice messages you send and text Cubbie indexes for search, which can include course material and mail text.
Transcribes voice messages and builds search embeddings. Vertex AI does not use this content to train its models.
Current
Exchange Token and OpenRouter
Your messages and the content Cubbie works with to answer them, including course material and mail, files and calendar entries from a connected Google account.
Model providers that run the large language models (currently OpenAI's GPT models) that generate Cubbie's replies, summaries and memory.
Current
Typesafe
Your message, Cubbie's draft reply and the content it was based on, which can include Google user data.
Checks that a reply is supported by the content it cites.
Current
Modal
Files in your Cubbie workspace and memory.
Stores your Cubbie workspace and runs the isolated environments in which Cubbie works with files.
Current
Photon
The phone number you bind and the content of the iMessage messages you exchange with Cubbie.
Delivers and receives messages on the iMessage channel.
Current
Clerk
Your name, email address, phone number and sign-in records.
Account sign-in and authentication.
Current
Cloudflare
Requests between your browser or the Chrome extension and our servers, including their content and your IP address.
Serves the website and carries API traffic.
Current
Mixpanel
A pseudonymous account ID, school, connection status and product events. No message, mail, file or course content.
Product usage analytics.
Current
Google Analytics
Pages visited on cubbie.ai, campaign parameters, IP address and device information.
Website analytics.
Current
Sentry
Error reports from the website and Chrome extension, including IP address and device information.
Diagnosing errors.
Current
Datadog
Service logs and traces, which can include message content.
Monitoring service health.
Being retired
Our infrastructure runs in the United States. Where we transfer personal data out of your region, we rely on appropriate safeguards such as standard contractual clauses.
Security
We protect your data with the following measures:
In transit.
Traffic between you and our services, and from our services to third-party APIs, is encrypted with TLS 1.2 or above.
At rest.
OAuth access and refresh tokens are encrypted with AES-256-GCM before they are written to the database. The underlying database and object storage are additionally encrypted at rest by our cloud provider with AES-256.
Key management.
Encryption keys and service credentials live in a dedicated secrets manager, separate from the application code and from the data they protect.
Access control.
Production access follows least privilege, requires multi-factor authentication, and is logged. Credential use is recorded in a per-connection access log.
Vulnerability management.
Dependencies are scanned for known vulnerabilities on every change and weekly; changes reach production through pull requests and automated testing.
Incident response.
We investigate security incidents and will notify affected users and regulators within the timeframes required by applicable law.
Independent assessment.
We complete an annual CASA security assessment covering our use of Google restricted scopes.
No method of transmission over the Internet or electronic storage is 100% secure, and we cannot guarantee absolute security.
Data Retention
We retain personal data only for as long as necessary for the purposes set out in this policy. Account information, conversations, memory, Canvas data and messages exchanged over the iMessage channel are kept while your account is active and deleted when you delete your account. Mail synced from a Google account is kept until you disconnect that account or delete your Cubbie account. Files Cubbie downloads or generates for you expire after 30 days. Product analytics events are linked to a pseudonymous identifier rather than your name or email address. We may retain certain information longer when required by a legal obligation.
Deleting Your Data and Revoking Access
Disconnecting Google.
When you disconnect a Google account in Cubbie's settings, we automatically revoke the token with Google, delete the stored credentials, and delete the mail synced from that account. Content from that account that already appears in your conversation history and memory remains until you delete your Cubbie account. A record that the deletion ran is kept for compliance.
Revoking access at Google.
If you instead revoke Cubbie's access at
your Google Account permissions page
, Cubbie can no longer reach your account and stops syncing. Mail already synced is deleted when you disconnect the account in Cubbie, when you delete your Cubbie account, or when you ask us to delete it.
Deleting your account.
Deleting your Cubbie account revokes all of its Google authorisations and permanently deletes your account, conversations and memory, synced mail, Canvas data, and the content of your iMessage messages. This cannot be undone.
Backups.
Deleted data can remain in our database's encrypted recovery backups until they expire, within 14 days.
Unbinding iMessage.
You can unbind the iMessage channel at any time from Cubbie's settings. As soon as the binding ends, messages from your number are no longer passed to Cubbie's assistant; your message history is deleted when you delete your account.
Your Rights
You can access, update, or delete your information from your account settings, or request that we assist in deleting it by contacting us. We may need to retain certain information to comply with legal obligations.
Cookies
We use necessary cookies for authentication and core functionality. Where required by law, non-essential cookies are used only with your consent. You can control cookies through your browser settings.
Children's Privacy
The Service does not address anyone under the age of 16. We do not knowingly collect personal data from children. If you believe a child has provided us personal data, please contact us and we will delete it.
Changes to This Policy
We may update this Privacy Policy from time to time. We will post the new policy on this page, update the "Last updated" date above, and record what changed below.
Date
Change
2026-09-17
First published.
2026-10-22
Added SMS verification data use and protections for mobile information and SMS opt-in consent.
Contact Us
If you have any questions about this Privacy Policy, contact us at
support@cubbie.io
.
Cubbie · Imaginix Inc.
Privacy
Terms
SMS Terms
