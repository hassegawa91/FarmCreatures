using FarmCreatures.World.Interaction;
using UnityEngine;

namespace FarmCreatures.Player
{
    [RequireComponent(typeof(PlayerController))]
    public class PlayerInteraction : MonoBehaviour
    {
        [Header("Interaction")]
        [SerializeField] private float interactionDistance = 1.2f;
        [SerializeField] private LayerMask interactionLayerMask = ~0;

        private PlayerController playerController;

        private void Awake()
        {
            playerController = GetComponent<PlayerController>();
        }

        private void Update()
        {
            if (Input.GetKeyDown(KeyCode.E))
            {
                TryInteract();
            }
        }

        private void TryInteract()
        {
            Vector2 origin = transform.position;
            Vector2 direction = playerController.FacingDirection;

            if (InteractionManager.Instance == null)
            {
                Debug.LogWarning("PlayerInteraction: InteractionManager não existe na cena.");
                return;
            }

            bool interacted = InteractionManager.Instance.TryInteract(
                gameObject,
                origin,
                direction,
                interactionDistance,
                interactionLayerMask
            );

            if (!interacted)
                Debug.Log("Nada para interagir.");
        }

        private void OnDrawGizmosSelected()
        {
            PlayerController controller = GetComponent<PlayerController>();
            Vector2 direction = controller != null ? controller.FacingDirection : Vector2.down;

            Gizmos.color = Color.yellow;
            Gizmos.DrawLine(transform.position, (Vector2)transform.position + direction.normalized * interactionDistance);
        }
    }
}
