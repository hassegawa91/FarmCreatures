using System.Collections;
using UnityEngine;

namespace FarmCreatures.Creatures
{
    [RequireComponent(typeof(Rigidbody2D))]
    public class CreatureController : MonoBehaviour
    {
        [SerializeField] private CreatureData data;
        [SerializeField] private float defaultMoveSpeed = 2f;
        [SerializeField] private float defaultWanderRadius = 4f;
        [SerializeField] private float defaultIdleTime = 2f;

        private Rigidbody2D rb;
        private Vector2 spawnPosition;
        private Vector2 targetPosition;
        private Coroutine wanderRoutine;

        private float MoveSpeed => data != null ? data.moveSpeed : defaultMoveSpeed;
        private float WanderRadius => data != null ? data.wanderRadius : defaultWanderRadius;
        private float IdleTime => data != null ? data.idleTime : defaultIdleTime;

        private void Awake()
        {
            rb = GetComponent<Rigidbody2D>();
            rb.gravityScale = 0f;
            rb.freezeRotation = true;
            spawnPosition = transform.position;
        }

        private void OnEnable()
        {
            wanderRoutine = StartCoroutine(WanderLoop());
        }

        private void OnDisable()
        {
            if (wanderRoutine != null)
                StopCoroutine(wanderRoutine);
        }

        private IEnumerator WanderLoop()
        {
            while (true)
            {
                PickNewTarget();

                while (Vector2.Distance(transform.position, targetPosition) > 0.1f)
                {
                    Vector2 direction = (targetPosition - (Vector2)transform.position).normalized;
                    rb.linearVelocity = direction * MoveSpeed;
                    yield return null;
                }

                rb.linearVelocity = Vector2.zero;
                yield return new WaitForSeconds(IdleTime);
            }
        }

        private void PickNewTarget()
        {
            Vector2 randomOffset = Random.insideUnitCircle * WanderRadius;
            targetPosition = spawnPosition + randomOffset;
        }
    }
}
