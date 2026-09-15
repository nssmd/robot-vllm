# Deployment boundary

Treat device configuration, driver plugins, model adapters and the runtime bearer
token as operator-controlled inputs. Model outputs cannot load plugins, select
network endpoints, change joint mappings, unlock resources, or supply evaluator
state. Keep simulator adjudication separate from policy observations.

The HTTP bearer token identifies one trusted application/operator boundary; this
release is not a multi-tenant authorization service. Bind locally or put it behind
an authenticated TLS proxy. Never expose unprotected model-serving ports to an
untrusted network. ROS goal-scoped heartbeats provide liveness, not authentication;
use a trusted ROS network and appropriate SROS2/network isolation for deployments.

The SQLite state directory supports one active coordinator on a Linux host. Do
not share it through network filesystems, run competing coordinators against one
physical actuator, or delete unresolved records to bypass quarantine. Robot-side
admission and commissioned stop behavior remain necessary.

No hardware emergency stop, synchronized physical motion, controller failover,
or collision avoidance is guaranteed by this orchestration layer. Native action
termination and a simulated hold are different from a commissioned hardware stop.

Do not publish credentials or sensitive episode imagery in an issue. Use GitHub's
private vulnerability reporting when available for security-sensitive findings.
