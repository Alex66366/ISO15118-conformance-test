Table 93 —Semantics and type definition for AC_EVSEStatusType


<table><tr><td>Element Name</td><td>Type</td><td>Semantics</td></tr><tr><td>RCD</td><td>simpleType
boolean
refer to Annex C.6 for the type definition</td><td>Indicates the current status of the Residual Current Device (RCD). If RCD is equal to true, the RCD has detected an error. If RCD is equal to false, the RCD has not detected an error. This status flag is for informational purpose only.</td></tr><tr><td>NotificationMaxDelay</td><td>simpleType
unsignedShort
This element is inherited from EVStatusType.
refer to Annex C.6 for the type definition</td><td>This value is the time in seconds from the point in time this message is sent (relative time) and expected to perform the action immediately. &quot;The SECC uses the NotificationMaxDelay element in the EVSEStatus to indicate the time until it expects the EVCC to react on the action request indicated in EVSENotification.</td></tr><tr><td>EVSENotification</td><td>simpleType
EVSENotificationType
Enumeration
This element is inherited from EVStatusType.
refer to Annex C.6 for the type definition</td><td>This value is used by the SECC to influence the behaviour of the EVCC. The EVSENotification contains an action that the SECC wants the EVCC to perform. The requested action is expected by the EVCC until the time provided in NotificationMaxDelay. If the target time is not in the future, the EVCC is expected to perform the action immediately.
During normal operation the value of EVSENotification is set to “none”.</td></tr></table>
