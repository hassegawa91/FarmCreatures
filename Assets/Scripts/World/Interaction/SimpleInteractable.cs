using UnityEngine;

namespace FarmCreatures.World.Interaction
{
    public class SimpleInteractable : MonoBehaviour, IInteractable
    {
        [SerializeField] private string interactionLabel = "Interagir";
        [SerializeField] private string debugMessage = "Objeto interagido.";

        public string InteractionLabel => interactionLabel;

        public void Interact(InteractionContext context)
        {
            Debug.Log($"{debugMessage} Ator: {context.actor.name}");
        }
    }
}
