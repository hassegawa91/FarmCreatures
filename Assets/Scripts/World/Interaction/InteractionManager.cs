using UnityEngine;

namespace FarmCreatures.World.Interaction
{
    public class InteractionManager : MonoBehaviour
    {
        public static InteractionManager Instance { get; private set; }

        private void Awake()
        {
            if (Instance != null && Instance != this)
            {
                Destroy(gameObject);
                return;
            }

            Instance = this;
        }

        public bool TryInteract(GameObject actor, Vector2 origin, Vector2 direction, float distance, LayerMask layerMask)
        {
            RaycastHit2D hit = Physics2D.Raycast(origin, direction.normalized, distance, layerMask);

            if (hit.collider == null)
                return false;

            IInteractable interactable = hit.collider.GetComponent<IInteractable>();

            if (interactable == null)
                interactable = hit.collider.GetComponentInParent<IInteractable>();

            if (interactable == null)
                return false;

            interactable.Interact(new InteractionContext(actor, direction.normalized));
            return true;
        }
    }
}
