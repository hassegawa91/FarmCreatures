using System.Collections;
using UnityEngine;

namespace FarmCreatures.Creatures
{
    [RequireComponent(typeof(Rigidbody2D))]
    public class CreatureController : MonoBehaviour
    {
        [Header("Data")]
        [SerializeField] private CreatureData data;

        [Header("Fallback Movement")]
        [SerializeField] private float defaultMoveSpeed = 2f;
        [SerializeField] private float defaultWanderRadius = 4f;
        [SerializeField] private float defaultIdleTime = 2f;

        [Header("Obstacle Avoidance")]
        [SerializeField] private LayerMask obstacleMask = ~0;
        [SerializeField] private float obstacleCheckDistance = 0.35f;
        [SerializeField] private float stuckCheckInterval = 0.75f;
        [SerializeField] private float stuckMinDistance = 0.05f;

        private Rigidbody2D rb;
        private Vector2 homePosition;
        private Vector2 targetPosition;
        private Coroutine wanderRoutine;
        private Vector2 lastPosition;
        private float stuckTimer;

        private float MoveSpeed => data != null ? data.moveSpeed : defaultMoveSpeed;
        private float WanderRadius => data != null ? data.wanderRadius : defaultWanderRadius;
        private float IdleTime => data != null ? data.idleTime : defaultIdleTime;

        private void Awake()
        {
            rb = GetComponent<Rigidbody2D>();
            rb.gravityScale = 0f;
            rb.freezeRotation = true;
            rb.collisionDetectionMode = CollisionDetectionMode2D.Continuous;

            homePosition = transform.position;
            lastPosition = transform.position;
        }

        private void OnEnable()
        {
            wanderRoutine = StartCoroutine(WanderLoop());
        }

        private void OnDisable()
        {
            if (wanderRoutine != null)
                StopCoroutine(wanderRoutine);

            if (rb != null)
                rb.linearVelocity = Vector2.zero;
        }

        private IEnumerator WanderLoop()
        {
            while (true)
            {
                PickNewTarget();

                while (Vector2.Distance(rb.position, targetPosition) > 0.12f)
                {
                    Vector2 direction = (targetPosition - rb.position).normalized;

                    if (IsBlocked(direction) || IsStuck())
                    {
                        rb.linearVelocity = Vector2.zero;
                        PickNewTarget();
                        yield return new WaitForSeconds(0.15f);
                        continue;
                    }

                    rb.linearVelocity = direction * MoveSpeed;
                    yield return null;
                }

                rb.linearVelocity = Vector2.zero;
                yield return new WaitForSeconds(IdleTime);
            }
        }

        private void PickNewTarget()
        {
            for (int i = 0; i < 10; i++)
            {
                Vector2 randomOffset = Random.insideUnitCircle * WanderRadius;
                Vector2 candidate = homePosition + randomOffset;
                Vector2 direction = (candidate - rb.position).normalized;

                if (!IsBlocked(direction))
                {
                    targetPosition = candidate;
                    return;
                }
            }

            targetPosition = homePosition;
        }

        private bool IsBlocked(Vector2 direction)
        {
            if (direction.sqrMagnitude < 0.01f)
                return false;

            RaycastHit2D hit = Physics2D.Raycast(rb.position, direction.normalized, obstacleCheckDistance, obstacleMask);

            if (hit.collider == null)
                return false;

            if (hit.collider.attachedRigidbody == rb)
                return false;

            return true;
        }

        private bool IsStuck()
        {
            stuckTimer += Time.deltaTime;

            if (stuckTimer < stuckCheckInterval)
                return false;

            float distanceMoved = Vector2.Distance(rb.position, lastPosition);
            lastPosition = rb.position;
            stuckTimer = 0f;

            return distanceMoved < stuckMinDistance;
        }
    }
}
