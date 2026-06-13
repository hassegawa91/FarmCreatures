using UnityEngine;

namespace FarmCreatures.Player
{
    [RequireComponent(typeof(Rigidbody2D))]
    public class PlayerController : MonoBehaviour
    {
        [Header("Movement")]
        [SerializeField] private float moveSpeed = 5f;
        [SerializeField] private float sprintMultiplier = 1.45f;

        private Rigidbody2D rb;
        private Vector2 movement;
        private Vector2 facingDirection = Vector2.down;

        public Vector2 Movement => movement;
        public Vector2 FacingDirection => facingDirection;

        private void Awake()
        {
            rb = GetComponent<Rigidbody2D>();
            rb.gravityScale = 0f;
            rb.freezeRotation = true;
        }

        private void Update()
        {
            movement.x = Input.GetAxisRaw("Horizontal");
            movement.y = Input.GetAxisRaw("Vertical");
            movement = movement.normalized;

            if (movement.sqrMagnitude > 0.01f)
                facingDirection = movement;
        }

        private void FixedUpdate()
        {
            float finalSpeed = moveSpeed;

            if (Input.GetKey(KeyCode.LeftShift) || Input.GetKey(KeyCode.RightShift))
                finalSpeed *= sprintMultiplier;

            rb.linearVelocity = movement * finalSpeed;
        }
    }
}
